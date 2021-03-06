#!/usr/bin/env python
# vim: tabstop=4 expandtab shiftwidth=4 softtabstop=4
# Author: "Chris Ward <cward@redhat.com>

import logging
logger = logging.getLogger(__name__)

from bson.objectid import ObjectId
from copy import copy
from datetime import datetime
import simplejson as json
import sys
import traceback

from metrique.tools.constants import UTC
from metrique.tools.json import Encoder

from metrique.server.config import mongodb
from metrique.server.defaults import MONGODB_CONF


class Job(object):
    def __init__(self, action, job_key=None):
        self._conf_mongodb = mongodb(MONGODB_CONF)
        self.c_job_activity = self._conf_mongodb.c_job_activity
        self.action = action
        self.objectid = job_key

        if job_key:
            # get previously init'd job...
            doc = self.c_job_activity.find_one(self._base_spec)
            if not doc:
                raise ValueError("Invalid job_key id: %s" % job_key)
            self.args = doc['args']
            self.created = doc['created']
            self.active = doc['active']
            self.error = doc['error']
        else:
            self.objectid = ObjectId()
            self.created = datetime.now(UTC)
            # which arguments are associated with this job
            self.args = None
            # will store datetime of completion, if there is one
            self.completed = None
            # default, we assume this job should run
            self.active = True
            # more defaults...
            self.error = None
            # initiate the job
            self.save()

    @property
    def _base_spec(self):
        return {'_id': self.objectid}

    def stop(self):
        logger.debug('STOP: %s' % self.objectid)
        self.active = False
        self.save()

    @property
    def payload(self):
        now = datetime.now(UTC)
        _payload = copy(self._base_spec)
        # certain content can have unpredictable keys names
        # that cause mongo issues if the data structures
        # are 'nested document' like so... dump as json
        _args = json.dumps(self.args, cls=Encoder, ensure_ascii=False)

        _payload.update({
            '_id': self.objectid,
            'atime': now,
            'created': self.created,
            'active': self.active,
            'completed': self.completed,
            'error': self.error,
            'class': str(self.__class__),
            'action': self.action,
            'args': _args,
        })
        return _payload

    def save(self):
        try:
            self.c_job_activity.save(self.payload, safe=True)
        except Exception as e:
            # oops! well, log the issue and update the process job
            # in the db with the error so clients are aware of the
            # issue.
            tb = traceback.format_exc(sys.exc_info())
            logger.error(tb)
            raise RuntimeError("Payload failed to save; dumping ERROR (%s)" % str(e))

    def complete(self):
        self.active = False
        now = datetime.now(UTC)
        if not self.completed:
            # don't overwrite the completed datetime
            # but add it if we're completed for the first time
            self.completed = now
        self.save()


def get_job(action, job_key=None):
    return Job(action, job_key)
