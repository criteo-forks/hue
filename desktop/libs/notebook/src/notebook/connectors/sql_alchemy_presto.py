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

import pyhive.presto

from notebook.connectors.sql_alchemy import SqlAlchemyApi, CONNECTION_CACHE

LOG_JSON_CACHE = {}
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

  if stage_info[u'done']:
    bytes_per_sec = '0'
    rows_per_sec = '0'
  else:
    bytes_per_sec = _format_human_readable(stage_info[u'processedBytes'] / elapsed_time_sec, 1024.0)
    rows_per_sec = _format_human_readable(stage_info[u'processedRows'] / elapsed_time_sec)

  if stage_info[u'state'] == u'FAILED':
    state = u'X'
  else:
    state = stage_info[u'state'][0]

  lines.append(STAGE_LINE.format(
    stage=name,
    state=state,
    rows=_format_human_readable(stage_info[u'processedRows']),
    rows_per_sec=rows_per_sec,
    bytes=_format_human_readable(stage_info[u'processedBytes'], 1024.0),
    bytes_per_sec=bytes_per_sec,
    queued=str(stage_info[u'queuedSplits']),
    run=str(stage_info[u'runningSplits']),
    done=str(stage_info[u'completedSplits']),
  ))
  for stage in stage_info[u'subStages']:
    _append_stage_lines(elapsed_time_sec, lines, indent + '  ', stage, first_line_index)


# Hack to report the response without disturbing Presto's response fetching
def _hack_presto_cursor():
  old_process_response = pyhive.presto.Cursor._process_response

  def _process_response(self, response):
    old_process_response(self, response)
    if hasattr(self, 'hue_guid'):
      LOG_JSON_CACHE[self.hue_guid] = response.json()

  pyhive.presto.Cursor._process_response = _process_response


_hack_presto_cursor()


class SqlAlchemyApiPresto(SqlAlchemyApi):
  def _extract_statement(self, snippet):
    # PyHive will try to '%' format the query even if there are no parameters
    # so we need to escape percent signs by doubling them
    return super(SqlAlchemyApiPresto, self)._extract_statement(snippet).replace('%', '%%')

  def close_statement(self, notebook, snippet):
    guid = snippet['result']['handle']['guid']
    if guid in LOG_JSON_CACHE:
      del LOG_JSON_CACHE[guid]
    return super(SqlAlchemyApiPresto, self).close_statement(notebook, snippet)

  def cancel(self, notebook, snippet):
    guid = snippet['result']['handle']['guid']
    if guid in LOG_JSON_CACHE:
      del LOG_JSON_CACHE[guid]
    return super(SqlAlchemyApiPresto, self).cancel(notebook, snippet)

  def get_log(self, notebook, snippet, startFrom=None, size=None):
    guid = snippet['result']['handle']['guid']
    log_json = LOG_JSON_CACHE.get(guid)
    # Only fetch log if currently executing
    if log_json:
      stats = log_json[u'stats']
      elapsed_time_sec = stats[u'elapsedTimeMillis'] / 1000
      duration_sec = int(elapsed_time_sec) % 60
      duration_min = int(elapsed_time_sec / 60)

      lines = []
      lines.append('Info url: {}'.format(log_json[u'infoUri']))
      lines.append('Query {query_id}, {state}, {nodes:,d} nodes'.format(
        query_id=log_json[u'id'],
        state=stats[u'state'],
        nodes=stats[u'nodes'],
      ))

      status_line = '{duration_min}:{duration_sec:02d} [{rows:5s} rows, {processed_bytes:6s}] [{rows_per_sec:5s} rows/s, {bytes_per_sec:8s}]'.format(
        duration_sec=duration_sec,
        duration_min=duration_min,
        rows=_format_human_readable(stats[u'processedRows']),
        processed_bytes=_format_human_readable(stats[u'processedBytes'], 1024.0, 'B'),
        rows_per_sec=_format_human_readable(stats[u'processedRows'] / elapsed_time_sec),
        bytes_per_sec=_format_human_readable(stats[u'processedBytes'] / elapsed_time_sec, 1024.0, 'B/s'),
      )
      if stats[u'state'] == u'FINISHED':
        status_line += ' 100%'
      elif stats[u'scheduled'] and stats[u'totalSplits']:
        status_line += ' {split_percent}%'.format(
          split_percent=int(min(99, stats[u'completedSplits'] * 100.0 / stats[u'totalSplits']))
        )
      lines.append(status_line)
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
      _append_stage_lines(elapsed_time_sec, lines, '', stats[u'rootStage'], len(lines))

      return '\n'.join(lines)
    else:
      return ''

