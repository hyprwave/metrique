#!/usr/bin/env pyehon
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward <cward@redhat.com>

from logging import getLogger
logger = getLogger(__name__)

from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, date
from datetime import time as dt_time
import re
import time

from metrique.server.drivers.basedriver import BaseDriver
from metrique.server.drivers.drivermap import get_cube, drivermap
from metrique.server.etl import get_last_id
from metrique.server.etl import save_doc, last_known_warehouse_mtime

from metrique.tools.constants import UTC
from metrique.tools.constants import LIST_TYPE, TUPLE_TYPE, INT_TYPE, FLOAT_TYPE
from metrique.tools.type_cast import type_cast

DEFAULT_ROW_LIMIT = 100000
MAX_WORKERS = 1


class BaseSql(BaseDriver):
    '''
    '''
    def __init__(self, host, db, row_limit=None,
                 *args, **kwargs):
        super(BaseSql, self).__init__(*args, **kwargs)
        self.db = db
        self.host = host
        if not row_limit:
            row_limit = DEFAULT_ROW_LIMIT
        self.row_limit = row_limit

    @property
    def proxy(self):
        raise NotImplementedError("BaseSql has not defined a proxy")

    def delta_activity(self, field):
        '''
        '''
        table = self.get_field_property('table', field)

        # activity 'when' field should always be the same across drivers
        cmp_field_lookup = self.get_field_property('lookup', 'when')

        last_update_dt = last_known_warehouse_mtime(self.name, field)
        if not last_update_dt:
            logger.debug('... This field has not yet completed a successful run')
            sql = None
        else:
            # NOTE: TEIID unable to parse miliseconds
            last_update = last_update_dt.strftime('%Y-%m-%d %H:%M:%S %z')

            # FIXME: let driver override dt_format
            dt_format = "yyyy-MM-dd HH:mm:ss z"
            sql = "%s.%s >= parseTimestamp('%s', '%s')" % (table,
                                                           cmp_field_lookup,
                                                           last_update,
                                                           dt_format)
        return sql

    def delta_mtime(self, field):
        '''
        '''
        sql = []
        _delta_mtime_dict = self.get_field_property('delta_mtime', field)
        if not _delta_mtime_dict:
            return None

        for mtime_field, mtime_column in _delta_mtime_dict.items():
            _sql = self._delta_mtime(mtime_field, mtime_column, field)
            if _sql:
                sql.append(_sql)
        if sql:
            return ' OR '.join(sql)
        else:
            return None

    def _delta_mtime(self, mtime_field, mtime_column, field):
        '''
        '''
        f_table = self.get_field_property('table', field)

        db = self.get_field_property('db', mtime_field)
        table = self.get_field_property('table', mtime_field)
        # if lookup isn't explicitly defined, use delta_mtime name
        lookup = self.get_field_property('lookup', mtime_field, mtime_field)

        # create SQL which will find only items with mtime column > last_update
        last_update_dt = last_known_warehouse_mtime(self.name, field)
        if not last_update_dt:
            logger.debug('... This field has not yet completed a successful run')
            sql = None
        else:
            # NOTE: TEIID SQL unable to parse miliseconds
            last_update_dt = last_update_dt.strftime('%Y-%m-%d %H:%M:%S %z')

            # FIXME: let driver override dt_format
            dt_format = "yyyy-MM-dd HH:mm:ss z"
            sql = """(%s.%s IN (
                        SELECT %s.%s FROM %s.%s
                        WHERE %s.%s >= parseTimestamp('%s', '%s')))
                    """ % (f_table, mtime_column,
                           table, mtime_column, db, table,
                           table, lookup,
                           last_update_dt,
                           dt_format)
        return sql

    def delta_history(self, field):
        '''
        '''
        logger.debug('Delta History')
        # return None tells ETL to ignore this filter
        # this is the field id that maps field name to activity history 'field affected' id
        last_update_dt = last_known_warehouse_mtime(self.name, field)
        if not last_update_dt:
            logger.debug('... This field has not yet completed a successful run')
            return None

        # Expecting a tuple with driver and the _id column
        cube, _column = self.get_field_property('delta_history', field)
        driver = drivermap[cube]

        logger.debug("Delta driver: %s" % driver)
        logger.debug("Delta driver.column: %s" % _column)

        # this is the activity driver's table
        table = driver.get_field_property('table')

        # this is the activity driver's when lookup column
        when = driver.get_field_property('lookup', 'when')
        # this is the activity driver's what lookup column
        f_what = self.get_field_property('what', field)

        # FIXME: TEIID SQL unable to parse miliseconds
        last_update_dt = last_update_dt.strftime('%Y-%m-%d %H:%M:%S %z')
        # FIXME: let driver override dt_format
        dt_format = "yyyy-MM-dd HH:mm:ss z"

        f_what_sql = ["%s.%s >= parseTimestamp('%s', '%s')" % (table, when, last_update_dt, dt_format)]
        if f_what:
            # this is the activity driver's what lookup column
            what = driver.get_field_property('lookup', 'what')

            trash, f_what = f_what
            try:  # wrap in quotes, if not an number
                int(f_what)
            except (TypeError, ValueError):
                f_what = "'%s'" % f_what

            f_what_sql.append('%s.%s = %s' % (table, what, f_what))

        # this is the activity driver's db
        db = driver.get_field_property('db')
        f_table = self.get_field_property('table', field)
        f_column = self.get_field_property('column', field)

        sql = """(%s.%s IN (SELECT DISTINCT %s.%s
                            FROM %s.%s
                            WHERE %s))""" % (f_table, f_column, table, _column,
                                             db, table, ' AND '.join(f_what_sql))
        return sql

    def _sql_fetchall(self, sql, start, field, row_limit):
        '''
        '''
        logger.debug('Fetching rows')

        # return the raw as token if no convert is defined by driver (self)
        convert = self.get_field_property('convert', field, None)

        # if driver.field specifies a type for this field, use it
        # otherwise, it'll be casted into a unicode string
        token_type = self.get_field_property('type', field)
        logger.debug('... Field Token Type: %s - %s' % (field, token_type))

        rows = list(self.proxy.fetchall(sql, row_limit, start))
        k = len(rows)

        logger.debug('... fetched (%i)' % len(rows))
        if not rows:
            return []

        logger.debug('Preparing row data...')
        t0 = time.time()
        _rows = []

        for row in rows:
            _rows.append(self._get_row(row, field,
                         convert, token_type))

        t1 = time.time()
        logger.info('... Rows prepared %i docs (%i/sec)' % (
            k, float(k) / (t1 - t0)))
        return _rows

    def _get_row(self, row, field, convert, token_type):
        # id 'column' is expected first
        id = row[0]
        # and raw token 'lookup' second
        raw = row[1]
        if type(raw) is date:
            # force convert dates into datetimes... otherwise mongo barfs
            raw = datetime.combine(raw, dt_time()).replace(tzinfo=UTC)
        # convert based on driver defined conversion method
        # and cast to appropriate data type
        if convert:
            tokens = convert(self, raw)
        else:
            tokens = raw
        tokens = type_cast(tokens, token_type)

        return {'id': id, 'field': field, 'tokens': tokens}

    def grouper(self, rows):
        ''' Group tokens by id/field '''
        k = len(rows)
        logger.debug('... ... ... Grouping started of %s rows!' % k)
        grouped = {}
        t0 = time.time()
        for row in rows:
            id = row['id']
            field = row['field']
            tokens = row['tokens']
            grouped.setdefault(id, {})
            grouped[id].setdefault(field, [])
            if not tokens:  # if tokens is empty, don't update the list
                continue
            grouped[id][field].append(tokens)
        t1 = time.time()
        logger.info('... ... ... Grouped %i docs (%i/sec)' % (
            k, float(k) / (t1 - t0)))
        return grouped

    def extract_func(self, **kwargs):
        with ProcessPoolExecutor(MAX_WORKERS) as executor:
            future = executor.submit(_extract_func, self.name, **kwargs)
        return future.result()


def _extract_func(cube, **kwargs):
    '''
    SQL import method
    '''
    c = get_cube(cube)
    field = kwargs.get('field')
    if not field:
        raise ValueError("Field argument required")
    force = int(kwargs.get('force', 0))
    id_delta = kwargs.get('id_delta', None)

    db = c.get_field_property('db', field)
    table = c.get_field_property('table', field)
    db_table = '%s.%s' % (db, table)
    column = c.get_field_property('column', field)
    table_column = '%s.%s' % (table, column)

    # max number of rows to return per call (ie, LIMIT)
    row_limit = c.get_field_property('row_limit', field, c.row_limit)
    try:
        row_limit = int(row_limit)
    except (TypeError, ValueError):
        raise ValueError("row_limit must be a number")

    _sql = c.get_field_property('sql', field)
    sql_where = []
    if _sql:
        sql = 'SELECT %s, %s FROM ' % (table_column, _sql[0])
        _from = [db_table]
        if _sql[1]:
            _from.extend(_sql[1])
        sql += ', '.join(_from)
        sql += ' '
        if _sql[2]:
            sql += ' '.join(_sql[2])
        sql += ' '
        if _sql[3]:
            sql_where.append('(%s)' % ' OR '.join(_sql[3]))

        delta_filter = []
        delta_filter_sql = None

        # force full update
        if force:
            _delta = False
        else:
            _delta = c.get_field_property('delta', field, True)

        if _delta:
            # delta is enabled
            # the following deltas are mutually exclusive
            if id_delta:
                delta_sql = "(%s IN (%s))" % (table_column, id_delta)
                delta_filter.append(delta_sql)
            elif c.get_field_property('delta_new_ids', field):
                # if we delta_new_ids is on, but there is no 'last_id',
                # then we need to do a FULL run...
                last_id = get_last_id(c.name, field)
                if last_id:
                    # FIXME: any reason to ensure we know what the _id is typecasted as?
                    try:
                            last_id = int(last_id)
                    except (TypeError, ValueError):
                            pass

                    if type(last_id) in [INT_TYPE, FLOAT_TYPE]:
                        last_id_sql = "(%s > %s)" % (table_column, last_id)
                    else:
                        last_id_sql = "(%s > '%s')" % (table_column, last_id)
                    delta_filter.append(last_id_sql)

                    # activity history driver's self check for updates
                    if c.get_field_property('delta_activity', field):
                        pda_sql = c.delta_activity(field)
                        if pda_sql:
                            delta_filter.append(pda_sql)

                    # driver can find changes by checking mtime field
                    if c.get_field_property('delta_mtime', field):
                        pmt_sql = c.delta_mtime(field)
                        if pmt_sql:
                            delta_filter.append(pmt_sql)

                    # field check if new history added
                    if c.get_field_property('delta_history', field):
                        pdh_sql = c.delta_history(field)
                        if pdh_sql:
                            delta_filter.append(pdh_sql)

        if delta_filter:
            delta_filter_sql = ' OR '.join(delta_filter)
            sql_where.append(delta_filter_sql)

    else:
        # default to field name for lookup column if none provided
        lookup = c.get_field_property('lookup', field, field)
        if type(lookup) in (LIST_TYPE, TUPLE_TYPE):
            x_table_column = table_column
            if len(lookup) == 2:
                _driver, _lookup = lookup
                # default join key is column if not otherwise defined
                _column = column
            elif len(lookup) == 3:
                _driver, _lookup, _column = lookup
            elif len(lookup) == 5:
                _driver, _lookup, _column, x_table, x_column = lookup
                x_table_column = '%s.%s' % (x_table, x_column)
            else:
                raise ValueError("Invalid lookup value")
            _driver = drivermap[_driver]
            #if _column != column:
            #    _column = _driver.get_field_property('column', _column)
            #print _column, column, '*'
            _db = _driver.get_field_property('db', _lookup)
            _table = _driver.get_field_property('table', _lookup)
            __lookup = _driver.get_field_property('lookup', _lookup, _lookup)
            table_lookup = '%s.%s' % (_table, __lookup)
            join_sql = ' LEFT JOIN %s.%s ON %s.%s = %s ' % (_db, _table,
                                                            _table, _column,
                                                            x_table_column)
        else:
            table_lookup = '%s.%s' % (table, lookup)
            join_sql = None

        # whether field has timezone info attached, assume datetime if so
        timezone = c.get_field_property('timezone', field)

        # sql column convert
        sql_column_convert = c.get_field_property('sql_column_convert', field)
        # sql lookup convert
        sql_lookup_convert = c.get_field_property('sql_lookup_convert', field)

        delta_filter = []
        delta_filter_sql = None

        # force full update
        if force:
            _delta = False
        else:
            _delta = c.get_field_property('delta', field, True)

        if _delta:
            # delta is enabled
            # the following deltas are mutually exclusive
            if id_delta:
                delta_sql = "(%s IN (%s))" % (table_column, id_delta)
                delta_filter.append(delta_sql)
            elif c.get_field_property('delta_new_ids', field):
                # if we delta_new_ids is on, but there is no 'last_id',
                # then we need to do a FULL run...
                last_id = get_last_id(c.name, field)
                if last_id:
                    # FIXME: any reason to ensure we know what the _id is typecasted as?
                    try:
                            last_id = int(last_id)
                    except (TypeError, ValueError):
                            pass

                    if type(last_id) in [INT_TYPE, FLOAT_TYPE]:
                        last_id_sql = "(%s > %s)" % (table_column, last_id)
                    else:
                        last_id_sql = "(%s > '%s')" % (table_column, last_id)
                    delta_filter.append(last_id_sql)

                    # activity history driver's self check for updates
                    if c.get_field_property('delta_activity', field):
                        pda_sql = c.delta_activity(field)
                        if pda_sql:
                            delta_filter.append(pda_sql)

                    # driver can find changes by checking mtime field
                    if c.get_field_property('delta_mtime', field):
                        pmt_sql = c.delta_mtime(field)
                        if pmt_sql:
                            delta_filter.append(pmt_sql)

                    # field check if new history added
                    if c.get_field_property('delta_history', field):
                        pdh_sql = c.delta_history(field)
                        if pdh_sql:
                            delta_filter.append(pdh_sql)

        if delta_filter:
            delta_filter_sql = ' OR '.join(delta_filter)

        if sql_column_convert:
            table_column = 'CONVERT(%s, %s)' % (table_column, sql_column_convert)

        if sql_lookup_convert:
            table_lookup = 'CONVERT(%s, %s)' % (table_lookup, sql_lookup_convert)

        # convert lookups with tz data to UTC
        if timezone:
            #                              tz, from, to
            table_lookup = "modifytimezone(%s, '%s', 'GMT+0')" % (table_lookup,
                                                                  timezone)

        sql = "SELECT %s, %s FROM %s.%s " % (table_column,
                                             table_lookup,
                                             db, table)

        if join_sql:
            sql += join_sql

        if delta_filter_sql:
            sql_where.append(" (%s) " % delta_filter_sql)

    if sql_where:
        sql += ' WHERE %s ' % ' AND '.join(sql_where)

    if not c.get_field_property('no_sort', field, False):
        sql += " ORDER BY %s ASC" % table_column

    # whether to query for distinct rows only or not; default, no
    if c.get_field_property('distinct', field, False):
        sql = re.sub('^SELECT', 'SELECT DISTINCT', sql)

    start = 0
    saved = 0
    _stop = False
    rows = []
    failed = []

    # FIXME: prefetch the next set of rows while importing to mongo
    logger.debug('... ... Starting SQL fetchall routine!')

    container = c.get_field_property('container', field)

    while not _stop:
        rows = c._sql_fetchall(sql, start, field, row_limit)
        k = len(rows)
        if k > 0:
            logger.debug('... ... Starting Processer')
            grouped = c.grouper(rows)
            logger.debug('... ... Saving docs now!')
            t0 = time.time()
            _id_k = 0
            for _id in grouped.iterkeys():
                _id_k += 1
                for field in grouped[_id].iterkeys():
                    tokens = grouped[_id][field]
                    if not tokens:
                        tokens = None
                    elif container and type(tokens) is not list:
                        tokens = [tokens]
                    elif not container and type(tokens) is list:
                        if len(tokens) > 1:
                            raise TypeError(
                                "Tokens contains too many values (%s); "
                                "(set container=True?)" % (tokens))
                        else:
                            tokens = tokens[0]

                    try:
                        saved += save_doc(c.name, field, tokens, _id)
                    except Exception as e:
                        logger.error(
                            'Error saving (%s) %s: %s' % (tokens, _id, e))
                        saved = 0
                    if not saved:
                        failed.append(_id)
            t1 = time.time()
            logger.info('... ... Saved %i docs (%i/sec)' % (
                k, k / (t1 - t0)))
        else:
            logger.debug('... ... No rows; nothing to process')

        if k < row_limit:
            _stop = True
        else:
            start += k
            if k != row_limit:  # theoretically, k == row_limit
                logger.warn(
                    "rows count seems incorrect! row_limit: %s, row returned: %s" % (
                        row_limit, k))

    result = {'saved': saved}
    if failed:
        result.update({'failed_ids': failed})
    return result