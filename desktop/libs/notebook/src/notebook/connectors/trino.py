#!/usr/bin/env python
# Licensed to Cloudera, Inc. under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  Cloudera, Inc. licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

'''
Paging is stateless: the position (`next_uri` + `row_count`) round-trips through
the client handle, so any web worker can serve any request. The Trino protocol
only allows each result page to be fetched once, plus retries of the current
page as long as its successor was never requested — fetch_result only ever
re-fetches the page it stopped on, which respects that.

On top of the stateless walk, rows passing through are copied into a bounded
result cache (django cache `trino_results`, keyed by Trino query id) so small
results can be downloaded again without re-running the query. An entry stops
growing once it exceeds cache_row_limit but keeps its contiguous prefix (a
single Trino page can hold thousands of rows, and the prefix is what makes a
download resumable). Pages are keyed by the URI they came from, which makes
caching idempotent when a page is seen twice (e.g. once by check_status and
again by fetch_result). A download first
requires the cache entry to have been produced by the statement being
downloaded (the frontend can send a handle pointing at an older execution),
then: a complete page chain is served entirely from the cache; a contiguous
prefix is served from the cache and the rest resumed from the live query (the
probing GET happens before anything is streamed, so failure falls back to a
plain re-execution); anything else re-executes the query within the download
request.

Reading pages consumes them, so after a resumed download the frontend's grid
can only keep paging through the cache: fetch_result and check_status serve
cached pages without hitting Trino, which works up to where the entry was
truncated — past it the grid gets an expired error, a deliberate trade-off
for downloads that never re-run the query.
'''

import json
import logging
import time
import textwrap
from urllib.parse import urlparse

import requests
from django.core.cache import caches
from django.utils.translation import gettext as _
from trino.auth import BasicAuthentication
from trino.client import ClientSession, TrinoQuery, TrinoRequest
from trino.exceptions import TrinoConnectionError

from beeswax import conf, data_export
from desktop.conf import AUTH_PASSWORD as DEFAULT_AUTH_PASSWORD, AUTH_USERNAME as DEFAULT_AUTH_USERNAME
from desktop.lib import export_csvxls
from desktop.lib.conf import coerce_password_from_script
from desktop.lib.i18n import force_unicode
from desktop.lib.rest.http_client import HttpClient, RestException
from desktop.lib.rest.resource import Resource
from desktop.settings import CACHES_TRINO_RESULTS_KEY
from notebook.connectors.base import Api, ExecutionWrapper, QueryError, ResultWrapper

LOG = logging.getLogger()
DEFAULT_CACHE_ROW_LIMIT = 2000
DEFAULT_FETCH_SIZE = 100
RESULT_CACHE_TTL = 60 * 60 * 24
POST_PAGE_KEY = 'POST'
STAGE_LINE = '{stage:10s}{state:1s}  {rows:5s}  {rows_per_sec:6s}  {bytes:5s}  {bytes_per_sec:7s}  {queued:6s}  {run:5s}  {done:5s}'


def _format_human_readable(amount, divisor=1000.0, suffix=''):
  for unit in ['', 'K', 'M', 'G', 'T', 'P']:
    if amount < divisor or unit == 'P':
      if amount < 10:
        fmt = "{:.2f}{}{}"
      elif amount < 100:
        fmt = "{:.1f}{}{}"
      else:
        fmt = "{:.0f}{}{}"
      return fmt.format(amount, unit, suffix)
    amount /= divisor


def _append_stage_lines(elapsed_time_sec, lines, indent, stage_info, first_line_index):
  name = indent + str(len(lines) - first_line_index)
  name += ''.join('.' for _ in range(max(0, 10 - len(name))))

  if stage_info['done']:
    bytes_per_sec = '0'
    rows_per_sec = '0'
  else:
    bytes_per_sec = _format_human_readable(stage_info['processedBytes'] / elapsed_time_sec, 1024.0)
    rows_per_sec = _format_human_readable(stage_info['processedRows'] / elapsed_time_sec)

  if stage_info['state'] == 'FAILED':
    state = 'X'
  else:
    state = stage_info['state'][0]

  lines.append(STAGE_LINE.format(
    stage=name,
    state=state,
    rows=_format_human_readable(stage_info['processedRows']),
    rows_per_sec=rows_per_sec,
    bytes=_format_human_readable(stage_info['processedBytes'], 1024.0),
    bytes_per_sec=bytes_per_sec,
    queued=str(stage_info['queuedSplits']),
    run=str(stage_info['runningSplits']),
    done=str(stage_info['completedSplits']),
  ))
  for stage in stage_info['subStages']:
    _append_stage_lines(elapsed_time_sec, lines, indent + '  ', stage, first_line_index)


def query_error_handler(func):
  def decorator(*args, **kwargs):
    try:
      return func(*args, **kwargs)
    except RestException as e:
      try:
        message = force_unicode(json.loads(e.message)['errors'])
      except Exception as ex:
        message = ex.message
      message = force_unicode(message)
      raise QueryError(message)
    except Exception as e:
      message = force_unicode(str(e))
      raise QueryError(message)
  return decorator


class TrinoApi(Api):
  def __init__(self, user, interpreter=None, request=None):
    Api.__init__(self, user, interpreter=interpreter, request=request)
    self.options = interpreter['options']
    self.cache_row_limit = self.options.get('cache_row_limit', DEFAULT_CACHE_ROW_LIMIT)
    self.server_host, self.server_port, self.http_scheme = self.parse_api_url(self.options.get('url'))
    self.catalog = self.options.get('catalog')
    self.source = self.options.get('source')
    self.auth = None

    auth_username = self.options.get('auth_username', DEFAULT_AUTH_USERNAME.get())
    auth_password = self.options.get('auth_password', self.get_auth_password())

    if self.request is not None and 'password' in self.request.session:
         auth_password = self.request.session['password']
         auth_username = self.user.username

    if auth_username and auth_password:
      self.auth_username = auth_username
      self.auth_password = auth_password
      self.auth = BasicAuthentication(self.auth_username, self.auth_password)


    trino_session = ClientSession(
      user.username,
      catalog=self.catalog,
      source=self.source,
    )
    self.trino_request = TrinoRequest(
      host=self.server_host,
      port=self.server_port,
      client_session=trino_session,
      http_scheme=self.http_scheme,
      auth=self.auth
    )

  def get_auth_password(self):
    auth_password_script = self.options.get('auth_password_script')
    return (
        coerce_password_from_script(auth_password_script)
        if auth_password_script
        else DEFAULT_AUTH_PASSWORD.get()
    )

  def _format_identifier(self, identifier, is_db=False):
    # Remove any backticks
    identifier = identifier.replace('`', '')

    # Check if already formatted
    if not (identifier.startswith('"') and identifier.endswith('"')):
      # Check if it's a multi-part identifier (e.g., catalog.schema)
      if '.' in identifier and is_db:
        # Split and format each part separately
        identifier = '"{}"'.format('"."'.join(identifier.split('.')))
      else:
        # Format single-part identifier
        identifier = f'"{identifier}"'

    return identifier

  @query_error_handler
  def parse_api_url(self, api_url):
    parsed_url = urlparse(api_url)
    return parsed_url.hostname, parsed_url.port, parsed_url.scheme

  @query_error_handler
  def create_session(self, lang=None, properties=None):
    pass

  def _format_result_meta(self, columns):
    return [{
        'name': col['name'],
        'type': col['type'],
        'comment': ''
      }
      for col in columns
    ] if columns else []

  def _result_cache(self):
    return caches[CACHES_TRINO_RESULTS_KEY]

  def _result_cache_key(self, guid):
    return 'trino_results:%s' % guid

  def _result_cache_init(self, status, meta, statement, database):
    entry = {
      # The statement/database the result came from: a cached result is only ever
      # served for the exact same statement, so a stale handle sent by the frontend
      # (pointing at an older execution) degrades to a re-execution, never to
      # another query's rows.
      'statement': (statement or '').strip(),
      'database': database,
      'meta': meta or [],
      'pages': {},
      'row_count': 0,
      'truncated': False,
      'stats': status.stats,
      'info_uri': status.info_uri,
    }
    self._entry_add_page(entry, POST_PAGE_KEY, status)
    self._result_cache().set(self._result_cache_key(status.id), entry, RESULT_CACHE_TTL)

  def _result_cache_add_page(self, guid, page_uri, status):
    if not guid:
      return
    cache = self._result_cache()
    key = self._result_cache_key(guid)
    entry = cache.get(key)
    if entry is None:
      # Only cache chains observed from their POST response on (created in execute()),
      # otherwise a worker joining mid-query could assemble a partial chain.
      return
    entry['stats'] = status.stats
    entry['info_uri'] = status.info_uri
    if not entry.get('meta') and getattr(status, 'columns', None):
      entry['meta'] = self._format_result_meta(status.columns)
    self._entry_add_page(entry, page_uri, status)
    cache.set(key, entry, RESULT_CACHE_TTL)

  def _entry_add_page(self, entry, page_uri, status):
    if entry.get('truncated') or page_uri in entry['pages']:
      return  # already cached: pages are keyed by URI so re-fetches are idempotent
    rows = list(status.rows or [])
    entry['pages'][page_uri] = {'rows': rows, 'next': status.next_uri}
    entry['row_count'] += len(rows)
    if entry['row_count'] > self.cache_row_limit:
      # Stop growing but keep what is stored: the contiguous prefix (including
      # the page that crossed the limit, e.g. a single fat page holding the
      # whole result) is what makes a later download resumable.
      entry['truncated'] = True

  def _get_cached_result_prefix(self, guid, statement, database):
    """Walk the cached page chain of the query from its POST page on.

    Returns (meta, rows, resume_uri) where rows is the contiguous cached prefix
    and resume_uri is the first page that never went through this cache (None
    when the whole chain is cached). Pages are consumed strictly in order, so
    resume_uri is exactly the next page a download may legally request from the
    live query. Returns None when the cache holds nothing usable for this
    statement."""
    if not guid:
      return None
    entry = self._result_cache().get(self._result_cache_key(guid))
    if not entry:
      LOG.info('Result cache miss for query %s: no entry' % guid)
      return None
    if entry.get('statement') != (statement or '').strip() or entry.get('database') != database:
      LOG.info('Result cache miss for query %s: cached for another statement' % guid)
      return None
    pages = entry.get('pages') or {}
    rows = []
    uri = POST_PAGE_KEY
    hops = 0
    while uri is not None:
      page = pages.get(uri)
      if page is None:
        return entry.get('meta') or [], rows, uri
      rows.extend(page['rows'])
      uri = page['next']
      hops += 1
      if hops > len(pages):
        LOG.warning('Result cache entry for query %s has a page cycle, ignoring it' % guid)
        return None
    return entry.get('meta') or [], rows, None

  def _get_complete_cached_result(self, guid, statement, database):
    """Return (meta, rows) if the entire page chain of the query is cached and it
    was produced by the same statement, else None."""
    prefix = self._get_cached_result_prefix(guid, statement, database)
    if prefix is None:
      return None
    meta, rows, resume_uri = prefix
    if resume_uri is not None:
      LOG.info('Result cache miss for query %s: page chain incomplete (%d rows cached)' % (guid, len(rows)))
      return None
    return meta, rows

  def _get_cached_page(self, guid, page_uri):
    """Return (page, entry) if this exact page is in the result cache, else None."""
    entry = self._get_cached_query_state(guid)
    if not entry:
      return None
    page = (entry.get('pages') or {}).get(page_uri)
    if page is None:
      return None
    return page, entry

  def _get_cached_query_state(self, guid):
    if not guid:
      return None
    return self._result_cache().get(self._result_cache_key(guid))

  @query_error_handler
  def execute(self, notebook, snippet):
    database = snippet['database']
    database = self._format_identifier(database, is_db=True)
    query_client = TrinoQuery(self.trino_request, 'USE ' + database)
    query_client.execute()

    current_statement = self._get_current_statement(notebook, snippet)
    statement = current_statement['statement']
    query_client = TrinoQuery(self.trino_request, statement)
    response = self.trino_request.post(query_client.query)
    status = self.trino_request.process(response)
    meta = self._format_result_meta(status.columns)
    self._result_cache_init(status, meta, snippet.get('statement'), snippet.get('database'))

    response = {
      'row_count': 0,
      'next_uri': status.next_uri,
      'sync': None,
      'has_result_set': status.next_uri is not None,
      'guid': status.id,
      'result': {
        'has_more': status.id is not None,
        'data': status.rows,
        'meta': meta,
        'type': 'table'
      }
    }
    response.update(current_statement)

    return response

  @query_error_handler
  def check_status(self, notebook, snippet):
    response = {}
    status = 'expired'
    next_uri = snippet['result']['handle']['next_uri']

    # Do not return "success" as a status - hue frontend will query
    # "fetch_result_size" which is not implement here. "available"
    # is the correct status
    if next_uri is None:
      status = 'available'
    else:
      guid = snippet['result']['handle'].get('guid')
      cached = self._get_cached_page(guid, next_uri)
      if cached is not None:
        # The page was already consumed (e.g. by a download resuming this query)
        # and can no longer be polled from Trino, but its content is cached.
        page, entry = cached
        state = (entry.get('stats') or {}).get('state')
        if page['rows'] or page['next'] is None or state == 'FINISHED':
          response['status'] = 'available'
          response['next_uri'] = next_uri
        else:
          response['status'] = 'running'
          response['next_uri'] = page['next']
        return response
      _response = self.trino_request.get(next_uri)
      _status = self.trino_request.process(_response)
      has_rows = bool(getattr(_status, 'rows', None))
      if guid:
        self._result_cache_add_page(guid, next_uri, _status)
      if _status.stats['state'] == 'QUEUED':
        status = 'waiting'
      elif _status.stats['state'] == 'RUNNING' and has_rows:
        status = 'available'
      elif _status.stats['state'] == 'RUNNING':
        status = 'running'
      else:
        status = 'available'

    # When rows were found the page holding them is NOT consumed: its URI is
    # handed back so the next fetch_result re-fetches it and serves its rows.
    response['status'] = status
    response['next_uri'] = _status.next_uri if status != 'available' else next_uri
    return response

  @query_error_handler
  def can_start_over(self, notebook, snippet):
    guid = snippet['result']['handle'].get('guid')
    return self._get_complete_cached_result(guid, snippet.get('statement'), snippet.get('database')) is not None

  @query_error_handler
  def fetch_result(self, notebook, snippet, rows, start_over):
    handle = snippet['result']['handle']
    guid = handle.get('guid')
    next_uri = handle.get('next_uri')
    # Rows of the page at `next_uri` already returned by previous calls. That page
    # is fetched again (a legal retry: its successor is only ever requested once
    # the page is fully served) and the already-returned rows are skipped.
    served_of_page = handle.get('row_count', 0) or 0
    row_limit = rows if rows and rows > 0 else DEFAULT_FETCH_SIZE

    data = []
    meta = []
    if served_of_page == 0 and handle.get('result'):
      # Rows returned by the initial POST response, if any
      data = list(handle['result'].get('data') or [])
      meta = handle['result'].get('meta') or []

    while next_uri and len(data) < row_limit:
      cached = self._get_cached_page(guid, next_uri)
      if cached is not None:
        # Pages already consumed (e.g. by a download resuming this query) can no
        # longer be fetched from Trino, but are served back from the cache.
        page, entry = cached
        page_rows = page['rows']
        page_next = page['next']
        if not meta:
          meta = entry.get('meta') or []
      else:
        try:
          response = self.trino_request.get(next_uri)
        except requests.exceptions.RequestException as e:
          raise TrinoConnectionError("failed to fetch: {}".format(e))

        status = self.trino_request.process(response)
        self._result_cache_add_page(guid, next_uri, status)
        if getattr(status, 'columns', None):
          meta = self._format_result_meta(status.columns)
        page_rows = status.rows or []
        page_next = status.next_uri

      new_rows = page_rows[served_of_page:] if served_of_page else page_rows
      take = row_limit - len(data)
      if len(new_rows) > take:
        data.extend(new_rows[:take])
        served_of_page += take
        break  # stay on this page, the next call re-reads it and skips served rows
      data.extend(new_rows)
      served_of_page += len(new_rows)
      if page_next is None:
        next_uri = None
      elif len(data) < row_limit:
        next_uri = page_next
        served_of_page = 0
      # else: limit hit exactly at the end of the page; stay on the fully-served page

    return {
      'row_count': served_of_page,
      'next_uri': next_uri,
      'has_more': bool(next_uri),
      'data': data or [],
      'meta': meta or [],
      'type': 'table'
    }

  @query_error_handler
  def autocomplete(self, snippet, database=None, table=None, column=None, nested=None, operation=None):
    response = {}

    if database is None:
      response['databases'] = self._show_databases()
    elif table is None:
      response['tables_meta'] = self._show_tables(database)
    elif column is None:
      columns = self._get_columns(database, table)
      response['columns'] = [col['name'] for col in columns]
      response['extended_columns'] = [{
        'comment': col.get('comment'),
        'name': col.get('name'),
        'type': col['type']
      }
        for col in columns
      ]

    return response

  @query_error_handler
  def get_sample_data(self, snippet, database=None, table=None, column=None, nested=False, is_async=False, operation=None):
    statement = self._get_select_query(database, table, column, operation)
    query_client = TrinoQuery(self.trino_request, statement)
    query_client.execute()

    response = {
      'status': 0,
      'rows': [],
      'full_headers': []
    }
    response['rows'] = query_client.result.rows
    response['full_headers'] = query_client.columns

    return response

  def _get_select_query(self, database, table, column=None, operation=None, limit=100):
    if operation == 'hello':
      statement = "SELECT 'Hello World!'"
    else:
      database = self._format_identifier(database, is_db=True)
      table = self._format_identifier(table)
      column = '%(column)s' % {'column': self._format_identifier(column)} if column else '*'
      statement = textwrap.dedent('''\
          SELECT %(column)s
          FROM %(database)s.%(table)s
          LIMIT %(limit)s
          ''' % {
        'database': database,
        'table': table,
        'column': column,
        'limit': limit,
      })

    return statement

  def close_statement(self, notebook, snippet):
    try:
      if snippet['result']['handle']['next_uri']:
        self.trino_request.delete(snippet['result']['handle']['next_uri'])
      else:
        return {'status': -1}  # missing operation ids
    except Exception as e:
      if 'does not exist in current session:' in str(e):
        return {'status': -1}  # skipped
      else:
        raise e

    return {'status': 0}

  def close_session_idle(self, notebook, session):
    for snippet in notebook.get('snippets', []):
      try:
        if snippet.get('result') and snippet['result'].get('handle') and snippet['result']['handle'].get('guid'):
          self.close_statement(notebook, snippet)
      except Exception as e:
        LOG.exception('Error closing statement: %s' % str(e))
    return {'status': 0}

  def close_session(self, session):
    # Avoid closing session on page refresh or editor close for now
    pass

  def cancel(self, notebook, snippet):
    guid = snippet['result']['handle'].get('guid')
    if guid:
      self._result_cache().delete(self._result_cache_key(guid))
    try:
      if snippet['result']['handle']['next_uri']:
        self.trino_request.delete(snippet['result']['handle']['next_uri'])
      else:
        return {'status': -1}
    except Exception as e:
      if 'does not exist in current session:' in str(e):
        return {'status': -1}  # skipped
      else:
        raise e

  def _show_databases(self):
    catalogs = self._show_catalogs()
    databases = []

    if self.catalog:
      query_client = TrinoQuery(self.trino_request, 'SHOW SCHEMAS FROM ' + self.catalog)
      response = query_client.execute()
      databases += [f'{item}' for sublist in response.rows for item in sublist]
    else:
      for catalog in catalogs:
        query_client = TrinoQuery(self.trino_request, 'SHOW SCHEMAS FROM ' + catalog)
        response = query_client.execute()
        databases += [f'{catalog}.{item}' for sublist in response.rows for item in sublist]

    return databases

  def _show_catalogs(self):
    query_client = TrinoQuery(self.trino_request, 'SHOW CATALOGS')
    response = query_client.execute()
    res = response.rows
    catalogs = [item for sublist in res for item in sublist]

    return catalogs

  def _show_tables(self, database):
    database = self._format_identifier(database, is_db=True)
    query_client = TrinoQuery(self.trino_request, 'USE ' + database)
    query_client.execute()
    query_client = TrinoQuery(self.trino_request, 'SHOW TABLES')
    response = query_client.execute()
    tables = response.rows
    return [{
      'name': table[0],
      'type': 'table',
      'comment': '',
    }
      for table in tables
    ]

  def _get_columns(self, database, table):
    database = self._format_identifier(database, is_db=True)
    query_client = TrinoQuery(self.trino_request, 'USE ' + database)
    query_client.execute()
    table = self._format_identifier(table)
    query_client = TrinoQuery(self.trino_request, 'DESCRIBE ' + table)
    response = query_client.execute()
    columns = response.rows

    return [{
      'name': col[0],
      'type': col[1],
      'comment': '',
    }
      for col in columns
    ]

  def progress(self, notebook, snippet, logs=None):
    guid = snippet['result']['handle']['guid'] if snippet.get('result') and snippet['result'].get('handle') and \
      snippet['result']['handle'].get('guid') else None
    entry = self._get_cached_query_state(guid)
    stats = entry.get('stats') if entry else None

    if stats:
      if stats.get('state') == 'FINISHED':
        return 100
      if stats.get('scheduled') and stats.get('totalSplits'):
        return min(99, int(stats['completedSplits'] * 100.0 / stats['totalSplits']))

    return 0

  def get_log(self, notebook, snippet, startFrom=None, size=None):
    guid = snippet['result']['handle']['guid'] if snippet.get('result') and snippet['result'].get('handle') and \
      snippet['result']['handle'].get('guid') else None
    entry = self._get_cached_query_state(guid)
    stats = entry.get('stats') if entry else None

    if stats:
      elapsed_time_sec = stats.get('elapsedTimeMillis', 0) / 1000

      lines = []
      lines.append('Info url: {}'.format(entry.get('info_uri')))
      lines.append('Query {query_id}, {state}, {nodes:,d} nodes'.format(
        query_id=guid,
        state=stats.get('state', 'UNKNOWN'),
        nodes=stats.get('nodes', 0),
      ))

      if elapsed_time_sec > 0:
        duration_sec = int(elapsed_time_sec) % 60
        duration_min = int(elapsed_time_sec / 60)

        status_line = '{duration_min}:{duration_sec:02d} [{rows:5s} rows, {processed_bytes:6s}] [{rows_per_sec:5s} rows/s, {bytes_per_sec:8s}]'.format(
          duration_sec=duration_sec,
          duration_min=duration_min,
          rows=_format_human_readable(stats.get('processedRows', 0)),
          processed_bytes=_format_human_readable(stats.get('processedBytes', 0), 1024.0, 'B'),
          rows_per_sec=_format_human_readable(stats.get('processedRows', 0) / elapsed_time_sec),
          bytes_per_sec=_format_human_readable(stats.get('processedBytes', 0) / elapsed_time_sec, 1024.0, 'B/s'),
        )
        if stats.get('state') == 'FINISHED':
          status_line += ' 100%'
        elif stats.get('scheduled') and stats.get('totalSplits'):
          status_line += ' {split_percent}%'.format(
            split_percent=int(min(99, stats['completedSplits'] * 100.0 / stats['totalSplits']))
          )
        lines.append(status_line)

      if stats.get('rootStage'):
        lines.append('')
        lines.append(STAGE_LINE.format(
          stage='STAGE',
          state='S',
          rows='ROWS',
          rows_per_sec='ROWS/s',
          bytes='BYTES',
          bytes_per_sec='BYTES/s',
          queued='QUEUED',
          run='RUN',
          done='DONE',
        ))
        _append_stage_lines(elapsed_time_sec, lines, '', stats['rootStage'], len(lines))

      return '\n'.join(lines)
    else:
      return ''

  @query_error_handler
  def explain(self, notebook, snippet):
    statement = snippet['statement'].rstrip(';')
    explanation = ''

    if statement:
      try:
        database = snippet['database']
        database = self._format_identifier(database, is_db=True)
        TrinoQuery(self.trino_request, 'USE ' + database).execute()
        result = TrinoQuery(self.trino_request, 'EXPLAIN ' + statement).execute()
        explanation = result.rows
      except Exception as e:
        explanation = str(e)

    return {
      'status': 0,
      'explanation': explanation,
      'statement': statement
    }

  def download(self, notebook, snippet, file_format='csv'):
    result_wrapper = TrinoExecutionWrapper(self, notebook, snippet)

    max_rows = conf.DOWNLOAD_ROW_LIMIT.get()
    max_bytes = conf.DOWNLOAD_BYTES_LIMIT.get()

    content_generator = data_export.DataAdapter(result_wrapper, max_rows=max_rows, max_bytes=max_bytes)
    generator = export_csvxls.create_generator(content_generator, file_format)

    def logged_generator():
      # Streaming response: failures here happen outside the Django exception
      # middleware and would otherwise die silently with a truncated download.
      chunks = 0
      try:
        for chunk in generator:
          chunks += 1
          yield chunk
        LOG.info('Trino download of query %s completed: %s rows in %s chunks'
                 % (self._download_guid(snippet), content_generator.row_counter, chunks))
      except GeneratorExit:
        LOG.warning('Trino download of query %s: connection closed from outside after %s rows in %s chunks'
                    % (self._download_guid(snippet), content_generator.row_counter, chunks))
        raise
      except Exception:
        LOG.exception('Trino download of query %s failed after %s rows in %s chunks'
                      % (self._download_guid(snippet), content_generator.row_counter, chunks))
        raise

    return logged_generator()

  def _download_guid(self, snippet):
    handle = (snippet.get('result') or {}).get('handle') or {}
    return handle.get('guid')


class TrinoExecutionWrapper(ExecutionWrapper):

  def __init__(self, api, notebook, snippet, callback=None):
    ExecutionWrapper.__init__(self, api, notebook, snippet, callback)
    self.cached_result_returned = False

  def fetch(self, handle, start_over=None, rows=None):
    if self.cached_result_returned:
      # The complete cached result was already returned in one batch
      return ResultWrapper([], [], False)

    if start_over:
      snippet_handle = self.snippet['result'].get('handle') or {}
      guid = snippet_handle.get('guid')
      prefix = self.api._get_cached_result_prefix(guid, self.snippet.get('statement'), self.snippet.get('database'))
      if prefix is not None:
        meta, cached_rows, resume_uri = prefix
        if resume_uri is None:
          self.cached_result_returned = True
          LOG.info('Serving download of query %s from the result cache (%d rows)' % (guid, len(cached_rows)))
          return ResultWrapper(meta, cached_rows, False)
        resumed = self._try_resume(guid, meta, cached_rows, resume_uri)
        if resumed is not None:
          return resumed

      start_over = False
      handle = self.api.execute(self.notebook, self.snippet)
      self.snippet['result']['handle'] = handle
      LOG.info('Query %s is not resumable, re-executed for download as query %s' % (guid, handle.get('guid')))

      if self.callback and hasattr(self.callback, 'on_execute'):
        self.callback.on_execute(handle)

      self.should_close = True
      self._until_available()

    if self.snippet['result']['handle'].get('sync', False):
      result = self.snippet['result']['handle']['result']
    else:
      result = self.api.fetch_result(self.notebook, self.snippet, rows, start_over)
      self.snippet['result']['handle']['row_count'] = result['row_count']
      self.snippet['result']['handle']['next_uri'] = result['next_uri']

    return ResultWrapper(result.get('meta'), result.get('data'), result.get('has_more'))

  def _try_resume(self, guid, meta, cached_rows, resume_uri):
    """Serve the cached prefix and continue from the live query instead of
    re-executing. The probing GET happens before anything is streamed, so any
    failure (query expired, closed, or its pages consumed by someone else)
    falls back to a re-execution."""
    try:
      response = self.api.trino_request.get(resume_uri)
      status = self.api.trino_request.process(response)
    except Exception as e:
      LOG.info('Could not resume query %s for download (%s), re-executing' % (guid, e))
      return None

    self.api._result_cache_add_page(guid, resume_uri, status)
    if getattr(status, 'columns', None):
      meta = self.api._format_result_meta(status.columns)

    handle = self.snippet['result']['handle']
    handle['result'] = None  # the POST page rows are already part of the cached prefix
    handle['next_uri'] = status.next_uri
    handle['row_count'] = 0
    # The resumed query completes naturally once drained: never close it, the
    # frontend may still be paging the cached part of the result.
    LOG.info('Resuming download of query %s: %d rows from the cache, then the live query' % (guid, len(cached_rows)))
    return ResultWrapper(meta, cached_rows + list(status.rows or []), bool(status.next_uri))

  def _until_available(self):
    if self.snippet['result']['handle'].get('sync', False):
      return  # Request is already completed

    count = 0
    sleep_seconds = 1
    check_status_count = 0
    get_log_is_full_log = self.api.get_log_is_full_log(self.notebook, self.snippet)

    while True:
      response = self.api.check_status(self.notebook, self.snippet)
      old_uri = self.snippet['result']['handle']['next_uri']
      self.snippet['result']['handle']['next_uri'] = response['next_uri']
      if self.callback and hasattr(self.callback, 'on_status'):
        self.callback.on_status(response['status'])
      if self.callback and hasattr(self.callback, 'on_log'):
        log = self.api.get_log(self.notebook, self.snippet, startFrom=count)
        if get_log_is_full_log:
          log = log[count:]

        self.callback.on_log(log)
        count += len(log)

      if response['status'] not in ['waiting', 'running', 'submitted']:
        self.snippet['result']['handle']['next_uri'] = old_uri
        break
      check_status_count += 1
      if check_status_count > 5:
        sleep_seconds = 5
      elif check_status_count > 10:
        sleep_seconds = 10
      time.sleep(sleep_seconds)
