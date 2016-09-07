#!/usr/bin/env python

""" Oplog parser, and patcher of end data by oplog operations.
Oplog synchronization with initially loaded data stored in psql.
OplogParser -- class for basic oplog parsing
do_oplog_apply -- handling oplog and applying oplog ops func
sync_oplog -- find syncronization point in oplog for initially loaded data."""

__author__ = "Yaroslav Litvinov"
__copyright__ = "Copyright 2016, Rackspace Inc."
__email__ = "yaroslav.litvinov@rackspace.com"

from logging import getLogger
from gizer.collection_reader import CollectionReader
from gizer.psql_objects import load_single_rec_into_tables_obj
from gizer.psql_objects import cmp_psql_mongo_tables
from gizer.oplog_parser import OplogParser
from gizer.oplog_parser import exec_insert
from gizer.oplog_parser import Callback
from gizer.oplog_handlers import cb_insert
from gizer.oplog_handlers import cb_update
from gizer.oplog_handlers import cb_delete
from gizer.psql_cache import PsqlCacheTable
from gizer.oplog_sync_base import OplogSyncBase
from gizer.oplog_sync_base import DO_OPLOG_READ_ATTEMPTS_COUNT
from mongo_reader.prepare_mongo_request import prepare_mongo_request
from mongo_reader.prepare_mongo_request import prepare_oplog_request
from mongo_reader.prepare_mongo_request import prepare_oplog_request_filter
from mongo_reader.prepare_mongo_request import prepare_oplog_request_collection
from mongo_schema.schema_engine import create_tables_load_bson_data

SYNC_REC_COUNT_IN_ONE_BATCH = 100

class TsData:
    def __init__(self, oplog_name, ts, queries):
        self.oplog_name = oplog_name
        self.ts = ts
        self.queries = queries
        self.sync_start = None


class OplogSyncUnallignedData(OplogSyncBase):
    """ This syncronizer inteneded to syncronize unalligned data, which 
    produced by init load. In opposite to OplogSyncAllignedData this
    implementation is slower as it using deep syncronization algorithm 
    and doing more comparison checks and caching intermediate data in postgres.
    Unalligned data that synced at once can be in further be syncronized by
    OplogSyncAllignedData."""

    def __init__(self, psql_etl, psql, mongo_readers, oplog_readers,
                 schemas_path, schema_engines, psql_schema):
        """ params:
        psql_etl -- Postgres cursor wrapper
        psql -- Postgres cursor wrapper. Separate cursor.
        mongo_readers -- dict of mongo readers, one per collection
        oplog -- Mongo oplog cursor wrappper
        schemas_path -- Path with js schemas representing mongo collections
        psql_schema -- psql schema whose tables data to patch."""
        super(OplogSyncUnallignedData, self).\
            __init__(psql, mongo_readers, oplog_readers,
                     schemas_path, schema_engines, psql_schema)
        self.psql_etl = psql_etl

    def get_rec_ids(self, start_ts_dict):
        """Return {collection_name: {rec_id: bool}} """
        res = {}
        getLogger(__name__).\
            info('get_rec_ids - locate timestamps after ts:%s' % start_ts_dict)
        projection = {"ts":1, "ns":1, "op":1, 
                      "o2._id":1, "o._id":1, "o2.id":1, "o.id":1}
        # use projection with query as parser is running in dryrun mode and
        # just need to get a list of ids related to timestamps
        for name in self.oplog_readers:
            # separate query for every shard
            js_oplog_query = prepare_oplog_request(start_ts_dict[name])
            self.oplog_readers[name].make_new_request(js_oplog_query,
                                                      projection)
        # create oplog parser. note: cb_insert doesn't need psql object
        parser = OplogParser(self.oplog_readers, self.schemas_path,
                             Callback(cb_insert, ext_arg=self.psql_schema),
                             Callback(cb_update, 
                                      ext_arg=(self.psql, self.psql_schema)),
                             Callback(cb_delete, 
                                      ext_arg=(self.psql, self.psql_schema)),
                             dry_run = True)
        # go over oplog get just rec ids for timestamps that can be handled
        oplog_queries = parser.next()
        while oplog_queries != None:
            collection_name = parser.item_info.schema_name
            rec_id = parser.item_info.rec_id
            if collection_name not in res:
                res[collection_name] = {}
            res[collection_name][rec_id] = None
            oplog_queries = parser.next()
        return res

    def _sync_oplog(self, start_ts_dict):
        """ Get syncronization point for every record of oplog and psql data
        (which usually is initially loaded data). 
        Return dict with timestamps if synchronization successfull, or None if not.
        params:
        start_ts_dict -- Dict with initial timestamps"""
        # create table to save map of syncronization for all records
        psql_cache_table = PsqlCacheTable(self.psql_etl.cursor,
                                          self.psql_schema)
        collection_rec_ids_dict = self.get_rec_ids(start_ts_dict)
        # get sync map. Every collection items have differetn sync point
        # and should be aligned
        for collection, rec_ids_dict in collection_rec_ids_dict.iteritems():
            sync_engine = OplogSyncEngine(start_ts_dict,
                                          collection, 
                                          self.schemas_path,
                                          self.schema_engines[collection],
                                          self.mongo_readers[collection],
                                          self.oplog_readers,
                                          self.psql,
                                          self.psql_schema,
                                          psql_cache_table)
            res = sync_engine.sync_collection_timestamps(sync_engine, rec_ids_dict)
            if not res:
                return None
        # get max sync_start timestamp for every shard to align sync_point
        max_sync_ts = {}
        for oplog_name in self.oplog_readers:
            sync_ts = psql_cache_table.select_max_synced_ts_at_shard(oplog_name)
            if sync_ts:
                max_sync_ts[oplog_name] = sync_ts
            else:
                max_sync_ts[oplog_name] = start_ts_dict[oplog_name]
        # iterate over all collections/rec_ids and execute rec related queries
        # up to speific alignment timestamp 
        getLogger(__name__).info("Sync query exec")
        for collection, rec_ids_dict in collection_rec_ids_dict.iteritems():
            query_exec_progress = 0
            for rec_id in rec_ids_dict:
                query_exec_progress += 1
                if not query_exec_progress % 100:
                    getLogger(__name__).info( \
                        "Sync query exec progress for %s: %d / %d" %
                        (collection,
                         query_exec_progress,
                         len(rec_ids_dict)))
                ts_cache = psql_cache_table.select_ts_related_to_rec_id(
                    collection, rec_id)
                flag = False
                for ts_data in ts_cache:
                    # Start execute queries from sync_start
                    if ts_data.sync_start:
                        flag = True
                    if not flag:
                        continue
                    # if timestamp is exist then need to syncronise
                    if ts_data.ts:
                        if ts_data.ts <= max_sync_ts[ts_data.oplog]:
                            getLogger(__name__).debug("Sync ts op %s" %
                                                      ts_data.ts)
                            self.oplog_rec_counter += 1
                            for oplog_query in ts_data.queries:
                                self.queries_counter += 1
                                exec_insert(self.psql, oplog_query)
        getLogger(__name__).info('COMMIT')
        self.psql.conn.commit()
        getLogger(__name__).info("Synchronization finished ts=%s" % max_sync_ts)
        return max_sync_ts

    def sync(self, ts_start_dict):
        """ Sync oplog and postgres. The result of synchronization is a single
        timestamp from oplog. So if do apply to psql all timestamps going after
        that sync point and then compare affected psql and mongo records 
        they should be equal.  If TS is not located then synchronization failed.
        params:
        ts_start_dict -- dict contains one timestamp for every oplog shard
        which is start pos to find sync point"""
        getLogger(__name__).\
                info('Start oplog sync. Default ts:%s' % str(ts_start_dict))
        # ts_start is timestamp starting from which oplog records
        # should be applied to psql tables to locate ts which corresponds to
        # initially loaded psql data;
        # None - means oplog records should be tested starting from beginning
        if ts_start_dict is None:
            ts_start_dict = {}
            for oplog_name in self.oplog_readers:
                ts_start_dict[oplog_name] = None
        sync_ts_dict = self._sync_oplog(ts_start_dict)
        getLogger(__name__).info("sync ts is: %s" % sync_ts_dict)
        if sync_ts_dict:
            getLogger(__name__).info('Synced at ts:%s' % sync_ts_dict)
            return sync_ts_dict
        else:
            getLogger(__name__).error('Sync failed.')
            return None

class OplogSyncEngine:

    def __init__(self, start_ts_dict, collection_name,
                 schemas_path, schema_engine,
                 mongo_reader, oplog_readers,
                 psql, psql_schema, psql_cache_table):
        self.cache = None
        self.start_ts_dict = start_ts_dict
        self.collection_name = collection_name
        self.schemas_path = schemas_path
        self.schema_engine = schema_engine
        self.mongo_reader = mongo_reader
        self.oplog_readers = oplog_readers
        self.psql = psql
        self.psql_schema = psql_schema
        self.psql_cache_table = psql_cache_table
        self.collection_reader = CollectionReader(collection_name, 
                                                  schema_engine, mongo_reader)


    def sync_collection_timestamps(self, sync_engine, rec_ids_dict):
        """ Sync all the collection's items. Return True / False """
        count_synced = 0
        sync_attempt = 0
        while sync_attempt < DO_OPLOG_READ_ATTEMPTS_COUNT \
                and len(rec_ids_dict) != count_synced :
            sync_attempt += 1
            getLogger(__name__).info("Sync collection:%s attempt # %d/%d" % 
                                     (self.collection_name, 
                                      sync_attempt, DO_OPLOG_READ_ATTEMPTS_COUNT))
            rec_ids = []
            for rec_id, sync in rec_ids_dict.iteritems():
                if sync:
                    continue
                rec_ids.append(rec_id)
                if len(rec_ids) < SYNC_REC_COUNT_IN_ONE_BATCH:
                    # collect rec_ids for processing
                    continue
                synced_recs = \
                    sync_engine.sync_recs_save_tsdata_to_psql_cache(rec_ids)
                count_synced += len(synced_recs)
                del rec_ids[:]
                for synced_rec_id in synced_recs:
                    rec_ids_dict[synced_rec_id] = True # synced
                getLogger(__name__).info("sync map progress for %s is: %d / %d" %
                                         (self.collection_name,
                                          count_synced,
                                          len(rec_ids_dict)))
            # after loop ends check if there items left to be handled
            if rec_ids:
                synced_recs = \
                    sync_engine.sync_recs_save_tsdata_to_psql_cache(rec_ids)
                count_synced += len(synced_recs)
                for synced_rec_id in synced_recs:
                    rec_ids_dict[synced_rec_id] = True # synced
            getLogger(__name__).info("Synced %s %d of %d recs." % 
                                     (self.collection_name,
                                      count_synced, len(rec_ids_dict)))
        return len(rec_ids_dict) == count_synced

    def sync_recs_save_tsdata_to_psql_cache(self, rec_ids):
        """ Return list of recs wor which sync_start is located"""
        synced_recs = []
        recid_tsdata_dict = self.locate_recs_sync_points(rec_ids)
        for rec_id, ts_data_list in recid_tsdata_dict.iteritems():
            if type(ts_data_list) is bool:
                # this rec is already synced, has no timestamps list
                psql_data = PsqlCacheTable.PsqlCacheData(
                    None, None, self.collection_name, None, rec_id, True)
                self.psql_cache_table.insert(psql_data)
                synced_recs.append(rec_id)
                continue
            # cache timestamps data in psql
            for ts_data in ts_data_list:
                psql_data = PsqlCacheTable.PsqlCacheData(
                    ts_data.ts, ts_data.oplog_name, self.collection_name,
                    ts_data.queries, rec_id, ts_data.sync_start)
                self.psql_cache_table.insert(psql_data)
                if ts_data.sync_start:
                    synced_recs.append(rec_id)
        self.psql_cache_table.commit()
        return synced_recs

    def locate_recs_sync_points(self, rec_ids):
        """ Return timestamps data and recs sync points in following format:
        res = { rec_id: [ TsData list ] }"""
        res = {}
        mongo_objects = \
            self.collection_reader.get_mongo_table_objs_by_ids(rec_ids)
	recid_ts_queries = self.get_tsdata_for_recs(rec_ids)
        for rec_id, ts_data_list in recid_ts_queries.iteritems():
            if str(rec_id) in mongo_objects:
                mongo_obj = mongo_objects[str(rec_id)]
            else:
                # get empty object as doesn't exists in mongo
                mongo_obj = create_tables_load_bson_data(self.schema_engine, {})
            psql_obj = load_single_rec_into_tables_obj(
                self.psql, self.schema_engine, self.psql_schema, rec_id)
            # cmp mongo and psql records before applying timestamps
            # just to check if it is already synced
            if cmp_psql_mongo_tables(rec_id, mongo_obj, psql_obj):
                # rec_id not require to sync, set flag it's already synced
                # overwrite list of ts by assigning True 
                res[rec_id] = True
                continue
            # Apply timestamps and then cmp resulting psql object and mongo obj
            # If not equal then shift to next timestamp and do it again
            for query_idx in xrange(len(ts_data_list)):
                cur_idx = query_idx
                while cur_idx < len(ts_data_list):
                    ts_data = ts_data_list[cur_idx]
                    for single_query in ts_data.queries:
                        exec_insert(self.psql, single_query)
                    cur_idx += 1
                psql_obj = load_single_rec_into_tables_obj(
                    self.psql, self.schema_engine, self.psql_schema, rec_id)
                equal = cmp_psql_mongo_tables(rec_id, mongo_obj, psql_obj)
                # check if located ts, to be used as start point to apply ts
                if equal:
                    if rec_id not in res:
                        res[rec_id] = ts_data_list
                    res[rec_id][query_idx].sync_start = True
                    getLogger(__name__).info("rec_id sync point %s : [%s]->%s" % 
                                             (rec_id, 
                                              ts_data_list[query_idx].oplog_name,
                                              ts_data_list[query_idx].ts))
                self.psql.conn.rollback()
                if equal:
                    break
        return res

    def get_tsdata_for_recs(self, rec_ids):
        res = {}
        # issue separate requests to oplog readers
        for oplog_name in self.oplog_readers:
            if self.oplog_readers[oplog_name].real_transport():
                dbname = self.mongo_reader.settings_list[0].dbname
                # query timestamps only related to collection
                # do not rec_ids as mongodb is response to slow
                js_query = prepare_oplog_request_collection(
                    self.start_ts_dict[oplog_name],
                    dbname,
                    self.collection_name)
                # query timestamps only related to rec_ids
                #js_query = prepare_oplog_request_filter(
                #    self.start_ts_dict[oplog_name],
                #    dbname,
                #    self.collection_name,
                #    rec_ids)
            else:
                # mocked transport will return all the timestamps
                js_query = prepare_oplog_request(self.start_ts_dict[oplog_name])
            self.oplog_readers[oplog_name].make_new_request(js_query)
        # create oplog parser. note: cb_insert doesn't need psql object
        parser = OplogParser(self.oplog_readers, self.schemas_path,
                             Callback(cb_insert, ext_arg=self.psql_schema),
                             Callback(cb_update, 
                                      ext_arg=(self.psql, self.psql_schema)),
                             Callback(cb_delete, 
                                      ext_arg=(self.psql, self.psql_schema)),
                             dry_run = False)
        oplog_queries = parser.next()
        while oplog_queries != None:
            collection_name = parser.item_info.schema_name
            oplog_name = parser.item_info.oplog_name
            rec_id = parser.item_info.rec_id
            last_ts = parser.item_info.ts
            #### mock transports support /BEGIN/: filter out results
            if collection_name != self.collection_name or rec_id not in rec_ids:
                oplog_queries = parser.next()
                continue
            #### mock transports support /END/
            if rec_id not in res:
                res[rec_id] = []
            res[rec_id].append( TsData(oplog_name, last_ts, oplog_queries) )
            oplog_queries = parser.next()
        return res

