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

import copy
import math
from unittest.mock import MagicMock, Mock, patch

import pytest
from django.core.cache import caches
from django.test import TestCase

from beeswax import data_export
from desktop.auth.backend import rewrite_user
from desktop.lib.django_test_util import make_logged_in_client
from desktop.settings import CACHES_TRINO_RESULTS_KEY
from notebook.connectors.base import QueryError
from notebook.connectors.trino import TrinoApi, TrinoExecutionWrapper
from useradmin.models import User


class TestTrinoApi(TestCase):

  @classmethod
  def setup_class(cls):
    # Mock user and interpreter
    cls.client = make_logged_in_client(username="hue_test", groupname="default", recreate=True, is_superuser=False)
    cls.user = User.objects.get(username="hue_test")
    cls.interpreter = {
      'options': {
        'url': 'https://example.com:8080'
      }
    }
    # Initialize TrinoApi with mock user and interpreter
    cls.trino_api = TrinoApi(cls.user, interpreter=cls.interpreter)

  def test_format_identifier(self):
    # db name test
    test_cases = [
      ("my_db", '"my_db"'),
      ("my_catalog.my_db", '"my_catalog"."my_db"'),
    ]

    for database, expected_output in test_cases:
      assert self.trino_api._format_identifier(database, is_db=True) == expected_output

    # table name test
    test_cases = [
      ("io.airlift.discovery.store:name=dynamic,type=distributedstore", '"io.airlift.discovery.store:name=dynamic,type=distributedstore"'),
      ("table", '"table"'),
    ]

    for table, expected_output in test_cases:
      assert self.trino_api._format_identifier(table) == expected_output

  def test_parse_api_url(self):
    # Test parse_api_url method
    api_url = 'http://example.com:8080'
    expected_result = ('example.com', 8080, 'http')
    result = self.trino_api.parse_api_url(api_url)

    assert result == expected_result

  def test_autocomplete_with_database(self):
    with patch('notebook.connectors.trino.TrinoApi._show_databases') as _show_databases:
      _show_databases.return_value = [
        {'name': 'test_catalog1.test_db1'}, {'name': 'test_catalog2.test_db1'}, {'name': 'test_catalog2.test_db2'}
      ]
      snippet = {}
      response = self.trino_api.autocomplete(snippet)

      assert 'databases' in response   # Check if 'databases' key exists in the response
      assert (response['databases'] ==
      [{'name': 'test_catalog1.test_db1'}, {'name': 'test_catalog2.test_db1'}, {'name': 'test_catalog2.test_db2'}])

  def test_autocomplete_with_database_and_table(self):
    with patch('notebook.connectors.trino.TrinoApi._show_tables') as _show_tables:
      _show_tables.return_value = [
        {'name': 'test_table1', 'type': 'table', 'comment': ''},
        {'name': 'test_table2', 'type': 'table', 'comment': ''},
        {'name': 'test_table3', 'type': 'table', 'comment': ''}
      ]
      snippet = {}
      database = 'test_db1'
      response = self.trino_api.autocomplete(snippet, database)

      assert 'tables_meta' in response   # Check if 'table_meta' key exists in the response
      assert (response['tables_meta'] ==
      [
      {'name': 'test_table1', 'type': 'table', 'comment': ''},
      {'name': 'test_table2', 'type': 'table', 'comment': ''},
      {'name': 'test_table3', 'type': 'table', 'comment': ''}
      ])

  def test_autocomplete_with_database_table_and_column(self):
    with patch('notebook.connectors.trino.TrinoApi._get_columns') as _get_columns:
      _get_columns.return_value = [
        {'name': 'test_column1', 'type': 'str', 'comment': ''},
        {'name': 'test_column2', 'type': 'int', 'comment': ''},
        {'name': 'test_column3', 'type': 'int', 'comment': ''}
      ]
      snippet = {}
      database = 'test_db1'
      table = 'test_table1'
      response = self.trino_api.autocomplete(snippet, database, table)

      assert 'extended_columns' in response   # Check if 'extended_columns' key exists in the response
      assert (response['extended_columns'] ==
      [
      {'comment': '', 'name': 'test_column1', 'type': 'str'},
      {'comment': '', 'name': 'test_column2', 'type': 'int'},
      {'comment': '', 'name': 'test_column3', 'type': 'int'}
      ])

      assert 'columns' in response   # Check if 'columns' key exists in the response
      assert response['columns'] == ['test_column1', 'test_column2', 'test_column3']

  def test_get_sample_data_success(self):
    with patch('notebook.connectors.trino.TrinoQuery') as TrinoQuery:
      # Mock TrinoQuery object and its execute method
      query_instance = TrinoQuery.return_value
      query_instance.result.rows = [['value1', 'value2'], ['value3', 'value4']]
      query_instance.columns = [
        {'name': 'test_column1', 'type': 'string', 'comment': ''}, {'name': 'test_column2', 'type': 'string', 'comment': ''}
      ]

      # Call the get_sample_data method
      result = self.trino_api.get_sample_data(snippet={}, database='test_db', table='test_table')

      assert result['status'] == 0
      assert result['rows'] == [['value1', 'value2'], ['value3', 'value4']]
      assert (result['full_headers'] ==
      [{'name': 'test_column1', 'type': 'string', 'comment': ''}, {'name': 'test_column2', 'type': 'string', 'comment': ''}])

  def test_check_status_available(self):
    mock_trino_request = MagicMock()
    self.trino_api.trino_request = mock_trino_request

    # Configure the MagicMock object to return expected responses
    mock_trino_request.get.return_value = MagicMock()
    mock_trino_request.process.return_value = MagicMock(stats={'state': 'FINISHED'}, next_uri='http://url')

    # Call the check_status method
    result = self.trino_api.check_status(notebook={}, snippet={'result': {'handle': {'next_uri': 'http://url'}}})

    assert result['status'] == 'available'
    assert result['next_uri'] == 'http://url'

  def test_execute(self):
    with patch('notebook.connectors.trino.TrinoQuery') as TrinoQuery:
      # Mock TrinoQuery object and its methods
      mock_query_instance = TrinoQuery.return_value
      mock_query_instance.query = "SELECT * FROM test_table"
      mock_query_instance.execute.return_value = MagicMock(next_uri=None, id='123', rows=[], columns=[])

      mock_trino_request = MagicMock()
      self.trino_api.trino_request = mock_trino_request

      # Configure the MagicMock object to return expected responses
      # (plain attribute values only: the status fields end up pickled into the result cache)
      mock_trino_request.get.return_value = MagicMock()
      mock_trino_request.process.return_value = MagicMock(
        stats={'state': 'FINISHED'}, next_uri='http://url', id=123, rows=[], columns=[], info_uri='http://info'
      )

      # Call the execute method
      snippet = {
        'database': 'test_db',
        'statement': 'SELECT * FROM test_table;',
        'result': {'handle': {}}
      }
      result = self.trino_api.execute(notebook={}, snippet=snippet)

      expected_result = {
        'row_count': 0,
        'next_uri': 'http://url',
        'sync': None,
        'has_result_set': True,
        'guid': 123,
        'result': {
          'has_more': True,
          'data': [],
          'meta': [],
          'type': 'table'
        },
        'statement_id': 0, 'has_more_statements': False, 'statements_count': 1,
        'previous_statement_hash': 'd1c7e7dd8869098919761253c921eea865d48ca79d4e43092c321cfd',
        'start': {'row': 0, 'column': 0}, 'end': {'row': 0, 'column': 23}, 'statement': 'SELECT * FROM test_table'
      }
      assert result == expected_result

      # Test multiple query execution
      snippet = {
        'database': 'test_db',
        'statement': 'use test_db;\nshow tables',
        'result': {'handle': {}}
      }
      result = self.trino_api.execute(notebook={}, snippet=snippet)

      expected_result = {
        'row_count': 0,
        'next_uri': 'http://url',
        'sync': None,
        'has_result_set': True,
        'guid': 123,
        'result': {
          'has_more': True,
          'data': [],
          'meta': [],
          'type': 'table'
        },
        'statement_id': 0, 'has_more_statements': True, 'statements_count': 2,
        'previous_statement_hash': '793204944f1800a86d75684d4be11eccb03b35f68441febb1362fd35',
        'start': {'row': 0, 'column': 0}, 'end': {'row': 0, 'column': 12}, 'statement': 'use test_db'
      }
      assert result == expected_result

  def test_fetch_result(self):
    # Mock TrinoRequest object and its methods
    mock_trino_request = MagicMock()
    self.trino_api.trino_request = mock_trino_request

    # Configure the MagicMock object to return expected responses
    mock_trino_request.get.return_value = MagicMock()
    _columns = [{'comment': '', 'name': 'test_column1', 'type': 'str'}, {'comment': '', 'name': 'test_column2', 'type': 'str'}]

    mock_trino_request.process.side_effect = [
      MagicMock(
        stats={'state': 'RUNNING'}, next_uri='http://url1', id=123,
        rows=[['value1', 'value2'], ['value3', 'value4']], columns=_columns
      ),
      MagicMock(
        stats={'state': 'RUNNING'}, next_uri='http://url2', id=123,
        rows=[['value5', 'value6'], ['value7', 'value8']], columns=_columns
      ),
      MagicMock(
        stats={'state': 'FINISHED'}, next_uri=None, id=123,
        rows=[['value9', 'value10'], ['value11', 'value12']], columns=_columns
      )
    ]

    # Call the fetch_result method
    result = self.trino_api.fetch_result(
      notebook={}, snippet={'result': {'handle': {'next_uri': 'http://url', 'result': {'data': []}}}}, rows=0, start_over=False
    )

    expected_result = {
      # rows already served of the page the fetch stopped on (the last one here)
      'row_count': 2,
      'next_uri': None,
      'has_more': False,
      'data': [
        ['value1', 'value2'], ['value3', 'value4'], ['value5', 'value6'],
        ['value7', 'value8'], ['value9', 'value10'], ['value11', 'value12']
      ],
      'meta': [{
        'name': column['name'],
        'type': column['type'],
        'comment': ''
        } for column in _columns],
      'type': 'table'
    }

    assert result == expected_result
    assert len(result['data']) == 6
    assert len(result['meta']) == 2

  def test_get_select_query(self):
    # Test with specified database, table, and column
    database = '`test_schema.test_db`'
    table = '`test_table`'
    column = 'test_column'
    expected_statement = (
        'SELECT "test_column"\n'
        'FROM "test_schema"."test_db"."test_table"\n'
        'LIMIT 100\n'
    )
    assert (
      self.trino_api._get_select_query(database, table, column) ==
      expected_statement)

    # Test with default parameters
    database = 'test_db'
    table = 'test_table'
    expected_statement = (
        'SELECT *\n'
        'FROM "test_db"."test_table"\n'
        'LIMIT 100\n'
    )
    assert (
      self.trino_api._get_select_query(database, table) ==
      expected_statement)

  def test_explain(self):
    with patch('notebook.connectors.trino.TrinoQuery') as TrinoQuery:
      snippet = {'statement': 'SELECT * FROM tpch.sf1.partsupp LIMIT 100;', 'database': 'tpch.sf1'}
      output = [['Trino version: 432\nFragment 0 [SINGLE]\n    Output layout: [partkey, suppkey, availqty, supplycost, comment]\n    '
      'Output partitioning: SINGLE []\n    Output[columnNames = [partkey, suppkey, availqty, supplycost, comment]]\n    │   '
      'Layout: [partkey:bigint, suppkey:bigint, availqty:integer, supplycost:double, comment:varchar(199)]\n    │   '
      'Estimates: {rows: 100 (15.67kB), cpu: 0, memory: 0B, network: 0B}\n    └─ Limit[count = 100]\n       │   '
      'Layout: [partkey:bigint, suppkey:bigint, availqty:integer, supplycost:double, comment:varchar(199)]\n       │   '
      'Estimates: {rows: 100 (15.67kB), cpu: 15.67k, memory: 0B, network: 0B}\n       └─ LocalExchange[partitioning = SINGLE]\n          '
      '│   Layout: [partkey:bigint, suppkey:bigint, availqty:integer, supplycost:double, comment:varchar(199)]\n          │   '
      'Estimates: {rows: 100 (15.67kB), cpu: 0, memory: 0B, network: 0B}\n          └─ RemoteSource[sourceFragmentIds = [1]]\n'
      '                 Layout: [partkey:bigint, suppkey:bigint, availqty:integer, supplycost:double, comment:varchar(199)]\n\n'
      'Fragment 1 [SOURCE]\n    Output layout: [partkey, suppkey, availqty, supplycost, comment]\n    Output partitioning: SINGLE []\n'
      '    LimitPartial[count = 100]\n    │   Layout: [partkey:bigint, suppkey:bigint, availqty:integer, supplycost:double, '
      'comment:varchar(199)]\n    │   Estimates: {rows: 100 (15.67kB), cpu: 15.67k, memory: 0B, network: 0B}\n    └─ '
      'TableScan[table = tpch:sf1:partsupp]\n           Layout: [partkey:bigint, suppkey:bigint, availqty:integer, supplycost:double, '
      'comment:varchar(199)]\n           Estimates: {rows: 800000 (122.44MB), cpu: 122.44M, memory: 0B, network: 0B}\n           '
      'partkey := tpch:partkey\n           availqty := tpch:availqty\n           supplycost := tpch:supplycost\n           '
      'comment := tpch:comment\n           suppkey := tpch:suppkey\n\n']]
      # Mock TrinoQuery object and its execute method
      query_instance = TrinoQuery.return_value
      query_instance.execute.return_value = MagicMock(next_uri=None, id='123', rows=output, columns=[])

      # Call the explain method
      result = self.trino_api.explain(notebook=None, snippet=snippet)

      # Assert the result
      assert result['status'] == 0
      assert result['explanation'] == output
      assert result['statement'] == 'SELECT * FROM tpch.sf1.partsupp LIMIT 100'

      query_instance = TrinoQuery.return_value
      query_instance.execute.side_effect = Exception('Mocked exception')

      # Call the explain method
      result = self.trino_api.explain(notebook=None, snippet=snippet)

      # Assert the exception message
      assert result['explanation'] == 'Mocked exception'

  @patch('notebook.connectors.trino.DEFAULT_AUTH_USERNAME.get', return_value='mocked_username')
  @patch('notebook.connectors.trino.DEFAULT_AUTH_PASSWORD.get', return_value='mocked_password')
  def test_auth_username_and_auth_password_default(self, mock_default_username, mock_default_password):
    trino_api = TrinoApi(self.user, interpreter=self.interpreter)

    assert trino_api.auth_username == 'mocked_username'
    assert trino_api.auth_password == 'mocked_password'

  @patch('notebook.connectors.trino.DEFAULT_AUTH_USERNAME.get', return_value='mocked_username')
  @patch('notebook.connectors.trino.DEFAULT_AUTH_PASSWORD.get', return_value='mocked_password')
  def test_auth_username_custom(self, mock_default_username, mock_default_password):
    self.interpreter['options']['auth_username'] = 'custom_username'
    self.interpreter['options']['auth_password'] = 'custom_password'
    trino_api = TrinoApi(self.user, interpreter=self.interpreter)

    assert trino_api.auth_username == 'custom_username'
    assert trino_api.auth_password == 'custom_password'

  @patch('notebook.connectors.trino.DEFAULT_AUTH_PASSWORD.get', return_value='mocked_password')
  def test_auth_password_script(self, mock_default_password):
    interpreter = {
      'options': {
        'url': 'https://example.com:8080',
        'auth_password_script': 'custom_script'
      }
    }

    with patch('notebook.connectors.trino.coerce_password_from_script', return_value='custom_password_script'):
      trino_api = TrinoApi(self.user, interpreter=interpreter)
      assert trino_api.auth_password == 'custom_password_script'

  def test_get_log(self):
    notebook = {}
    snippet = {
      'result': {
        'handle': {
          'guid': '1234-abcd-5678-efgh'
        }
      }
    }

    # No cached state for this query: no log lines
    result = self.trino_api.get_log(notebook, snippet)

    assert result == ''


class _FakeTrinoStatus(object):
  def __init__(self, id, next_uri, rows, columns, stats):
    self.id = id
    self.next_uri = next_uri
    self.rows = rows
    self.columns = columns
    self.stats = stats
    self.info_uri = 'http://info/' + id


class _FakeTrinoServer(object):
  """Enforces the Trino REST protocol: each result page is consumed once; the
  current page may be re-fetched only until its successor is requested.

  Pages of a SELECT: page 0 (POST response, QUEUED, no rows), page 1 (RUNNING,
  no rows), pages 2..N (page_size rows each), last data page FINISHED with no
  next_uri. Non-SELECT statements (USE ...) finish in their POST response.
  """
  COLUMNS = [{'name': 'c1', 'type': 'bigint'}, {'name': 'c2', 'type': 'varchar'}]

  def __init__(self, total_rows, page_size, row_value=None):
    self.total_rows = total_rows
    self.page_size = page_size
    self.queries = {}
    self.qcount = 0
    self.select_count = 0
    self.get_count = 0
    self.row_value = row_value or (lambda i: [i, 'row-%d' % i])

  def _uri(self, qid, page):
    return 'http://coord/v1/statement/%s/%d' % (qid, page)

  def post(self, sql, additional_http_headers=None):
    self.qcount += 1
    qid = 'q%d' % self.qcount
    if sql.lstrip().upper().startswith('SELECT'):
      self.select_count += 1
    self.queries[qid] = {'sql': sql, 'max_served': -1}
    return ('response', qid, 0)

  def get(self, uri):
    qid, page = uri.rsplit('/', 2)[-2], int(uri.rsplit('/', 2)[-1])
    if page < self.queries[qid]['max_served']:
      raise Exception('410 Gone: page %d of %s already superseded' % (page, qid))
    self.get_count += 1
    return ('response', qid, page)

  def delete(self, uri):
    pass

  def process(self, response):
    _, qid, page = response
    q = self.queries[qid]
    q['max_served'] = max(q['max_served'], page)

    if not q['sql'].lstrip().upper().startswith('SELECT'):
      return _FakeTrinoStatus(qid, None, [], self.COLUMNS, {'state': 'FINISHED', 'elapsedTimeMillis': 1})

    n_data = max(1, math.ceil(self.total_rows / self.page_size))
    if page == 0:
      return _FakeTrinoStatus(qid, self._uri(qid, 1), [], None, {'state': 'QUEUED', 'elapsedTimeMillis': 1})
    if page == 1:
      return _FakeTrinoStatus(qid, self._uri(qid, 2), [], self.COLUMNS, {'state': 'RUNNING', 'elapsedTimeMillis': 1})
    data_idx = page - 2
    if data_idx < n_data:
      start = data_idx * self.page_size
      end = min(start + self.page_size, self.total_rows)
      rows = [self.row_value(i) for i in range(start, end)]
      state = 'RUNNING' if data_idx < n_data - 1 else 'FINISHED'
      next_uri = self._uri(qid, page + 1) if data_idx < n_data - 1 else None
      return _FakeTrinoStatus(qid, next_uri, rows, self.COLUMNS, {'state': state, 'elapsedTimeMillis': 1})
    return _FakeTrinoStatus(qid, None, [], self.COLUMNS, {'state': 'FINISHED', 'elapsedTimeMillis': 1})


class _FakeTrinoQuery(object):
  def __init__(self, request, sql):
    self.request = request
    self.query = sql

  def execute(self):
    return self.request.process(self.request.post(self.query))


class TestTrinoProtocolFlows(TestCase):
  """Drives the connector against a protocol-enforcing fake server: any fetch of
  a superseded page URI raises, so state desynchronization fails the test."""

  @classmethod
  def setup_class(cls):
    cls.client = make_logged_in_client(username="hue_test", groupname="default", recreate=True, is_superuser=False)
    cls.user = User.objects.get(username="hue_test")
    cls.interpreter = {'options': {'url': 'https://example.com:8080'}}

  def setUp(self):
    caches[CACHES_TRINO_RESULTS_KEY].clear()

  def _make_api(self, server):
    api = TrinoApi(self.user, interpreter=self.interpreter)
    api.trino_request = server
    return api

  def _execute(self, api, statement='SELECT * FROM t'):
    with patch('notebook.connectors.trino.TrinoQuery', _FakeTrinoQuery):
      snippet = {'database': 'db', 'statement': statement, 'result': {'handle': {}}}
      snippet['result']['handle'] = api.execute({}, snippet)
      return snippet

  def _poll(self, api, snippet):
    # the frontend persists next_uri from every check_status response
    response = api.check_status({}, snippet)
    snippet['result']['handle']['next_uri'] = response['next_uri']
    return response['status']

  def _poll_until_available(self, api, snippet):
    for _ in range(100):
      if self._poll(api, snippet) not in ('waiting', 'running', 'submitted'):
        return
    raise AssertionError('query never became available')

  def _fetch(self, api, snippet, rows=100):
    # the frontend persists row_count and next_uri from every fetch response
    result = api.fetch_result({}, snippet, rows, False)
    snippet['result']['handle']['row_count'] = result['row_count']
    snippet['result']['handle']['next_uri'] = result['next_uri']
    return result

  def _fetch_all(self, api, snippet, rows=100):
    all_rows = []
    for _ in range(200):
      result = self._fetch(api, snippet, rows=rows)
      all_rows.extend(result['data'])
      if not result['has_more']:
        return all_rows
    raise AssertionError('paging never ended')

  def _download(self, api, snippet):
    # a download is a separate HTTP request: it gets its own copy of the snippet,
    # the frontend's own handle is never mutated by it
    snippet = copy.deepcopy(snippet)
    with patch('notebook.connectors.trino.TrinoQuery', _FakeTrinoQuery), \
         patch('notebook.connectors.trino.time') as _time:
      _time.sleep = lambda seconds: None
      wrapper = TrinoExecutionWrapper(api, {}, snippet)
      adapter = data_export.DataAdapter(wrapper, max_rows=1000000, max_bytes=-1)
      rows = []
      for _headers, data in adapter:
        rows.extend(data)
      return rows

  def test_ui_paging_returns_all_rows_in_order(self):
    server = _FakeTrinoServer(1500, 400)
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)

    rows = self._fetch_all(api, snippet)

    assert [r[0] for r in rows] == list(range(1500))

  def test_ui_paging_with_batches_aligned_to_page_size(self):
    server = _FakeTrinoServer(2000, 500)
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)

    rows = self._fetch_all(api, snippet, rows=500)

    assert [r[0] for r in rows] == list(range(2000))

  def test_repeated_status_poll_causes_no_duplicates_or_drops(self):
    server = _FakeTrinoServer(1500, 400)
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)
    self._poll(api, snippet)  # e.g. a page refresh re-checks the same status URI

    rows = self._fetch_all(api, snippet)

    assert [r[0] for r in rows] == list(range(1500))

  def test_repetitive_row_content_is_not_dropped(self):
    # Identical rows on every page must not be mistaken for already-seen data
    server = _FakeTrinoServer(1200, 400, row_value=lambda i: ['x', 'y'])
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)

    rows = self._fetch_all(api, snippet)

    assert len(rows) == 1200

  def test_download_of_complete_cached_result_does_no_network_call(self):
    server = _FakeTrinoServer(300, 400)  # single data page: complete after polling
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)
    assert api.can_start_over({}, snippet)

    gets_before, selects_before = server.get_count, server.select_count
    rows = self._download(api, snippet)

    assert [r[0] for r in rows] == list(range(300))
    assert server.get_count == gets_before
    assert server.select_count == selects_before

  def test_download_of_fully_paged_result_is_served_from_cache(self):
    server = _FakeTrinoServer(1500, 400)
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)
    self._fetch_all(api, snippet)
    assert api.can_start_over({}, snippet)

    gets_before, selects_before = server.get_count, server.select_count
    rows = self._download(api, snippet)

    assert [r[0] for r in rows] == list(range(1500))
    assert server.get_count == gets_before
    assert server.select_count == selects_before

  def test_download_resumes_live_query_when_partially_cached(self):
    server = _FakeTrinoServer(5000, 400)  # 5000 > cache_row_limit
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)
    self._fetch(api, snippet, rows=100)
    assert not api.can_start_over({}, snippet)

    rows = self._download(api, snippet)

    assert [r[0] for r in rows] == list(range(5000))
    assert server.select_count == 1  # cached prefix + live continuation, no re-run

  def test_scroll_after_resumed_download_is_served_from_cache(self):
    server = _FakeTrinoServer(1500, 400)  # fits in cache_row_limit
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)
    self._fetch(api, snippet, rows=100)

    rows = self._download(api, snippet)
    assert [r[0] for r in rows] == list(range(1500))
    assert server.select_count == 1

    # The download consumed the live pages, but they all fit in the cache:
    # the grid keeps paging (from where it was) and a new download is free.
    scrolled = self._fetch_all(api, snippet)
    assert [r[0] for r in scrolled] == list(range(100, 1500))
    gets_before = server.get_count
    assert [r[0] for r in self._download(api, snippet)] == list(range(1500))
    assert server.get_count == gets_before

  def test_download_resumes_when_first_page_exceeds_cache_limit(self):
    # A single Trino page can hold more rows than cache_row_limit: the cached
    # prefix is kept (truncated entry) so the download still resumes.
    server = _FakeTrinoServer(2800, 2800)
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)
    self._fetch(api, snippet, rows=100)

    rows = self._download(api, snippet)

    assert [r[0] for r in rows] == list(range(2800))
    assert server.select_count == 1  # resumed, not re-executed

  def test_scroll_after_resumed_download_of_large_result_expires_past_cache(self):
    server = _FakeTrinoServer(5000, 400)  # exceeds cache_row_limit
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)
    self._fetch(api, snippet, rows=100)

    rows = self._download(api, snippet)
    assert [r[0] for r in rows] == list(range(5000))
    assert server.select_count == 1

    # Accepted trade-off: the grid keeps paging through the cached prefix, then
    # surfaces an error instead of silently wrong rows once past the truncation.
    scrolled = []
    with pytest.raises(QueryError):
      for _ in range(200):
        scrolled.extend(self._fetch(api, snippet, rows=100)['data'])
    assert scrolled == [[i, 'row-%d' % i] for i in range(100, 100 + len(scrolled))]
    assert len(scrolled) < 4900  # expired before reaching the end

  def test_download_falls_back_to_reexecute_when_resume_fails(self):
    server = _FakeTrinoServer(1500, 400)
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)
    self._fetch(api, snippet, rows=100)

    # Another consumer (e.g. an expired/raced query) makes the resume page unfetchable
    qid = snippet['result']['handle']['guid']
    for _ in range(2):
      server.process(server.get(server._uri(qid, server.queries[qid]['max_served'] + 1)))

    rows = self._download(api, snippet)

    assert [r[0] for r in rows] == list(range(1500))
    assert server.select_count == 2  # resume probe failed, fell back to a re-run

  def test_download_with_stale_handle_reexecutes_instead_of_serving_old_rows(self):
    # The frontend can send a snippet whose result handle still points at an older
    # execution: the rows cached for that older query must never be served.
    server = _FakeTrinoServer(300, 400)
    api = self._make_api(server)
    old_snippet = self._execute(api, statement='SELECT * FROM old_table')
    self._poll_until_available(api, old_snippet)
    assert api.can_start_over({}, old_snippet)  # the old result is fully cached

    stale_snippet = {
      'database': 'db',
      'statement': 'SELECT * FROM new_table',
      'result': {'handle': dict(old_snippet['result']['handle'])},
    }
    assert not api.can_start_over({}, stale_snippet)

    rows = self._download(api, stale_snippet)

    assert server.select_count == 2  # re-executed the new statement, no cache hit
    assert [r[0] for r in rows] == list(range(300))

  def test_download_reexecutes_on_worker_without_cached_state(self):
    server = _FakeTrinoServer(1500, 400)
    api = self._make_api(server)
    snippet = self._execute(api)
    self._poll_until_available(api, snippet)
    self._fetch_all(api, snippet)

    caches[CACHES_TRINO_RESULTS_KEY].clear()  # download lands on another worker

    rows = self._download(api, snippet)

    assert [r[0] for r in rows] == list(range(1500))
    assert server.select_count == 2
