from datetime import datetime, timedelta
import logging
import os
import uuid

from google.appengine.api import memcache
from google.appengine.ext import db
from google.appengine.api import taskqueue

from utils import *

def jsonable(value):
    if isinstance(value, db.Model):
        return value.key().name()
    elif value:
        return str(value)
    else:
        return None

class Pool(db.Model):
    creator = db.UserProperty(required=True)

    def to_dict(self):
        return { 'name': self.key().name() }

    @staticmethod
    def create(params, creator):
        pool = Pool(key_name=params['name'], creator=creator)
        pool.put()
        return pool

class Task(db.Model):
    title = db.StringProperty(required=True)
    description = db.TextProperty(required=True)
    url = db.LinkProperty(required=True)
    notify_url = db.LinkProperty()
    pool = db.ReferenceProperty(Pool)
    created = db.DateTimeProperty(auto_now_add=True)
    creator = db.UserProperty(required=True)
    assigned = db.DateTimeProperty()
    assigned_to = db.UserProperty()
    completed = db.DateTimeProperty()

    def to_dict(self):
        return dict([ (p, jsonable(getattr(self, p))) for p in [
            'title', 'description', 'url', 'pool', 'created', 'assigned', 'completed'
        ] ] + [ ('taskId', str(self.key())) ])

    def fullURL(self):
        return url_with_params(self.url, { 'taskId': self.key() })

    @staticmethod
    def create(params, creator):
        task = Task(title=params['title'],
                    description=params['description'],
                    url=params['url'],
                    notify_url=params.get('notifyUrl'),
                    creator=creator)
        if 'pool' in params:
            task.pool = Pool.get_by_key_name(params['pool'])
        task.put()
        taskqueue.add(url='/queue/notify', params={ 'task': task.key(), 'event': 'created' })
        return task

    def queue(self, countdown):
        taskqueue.add(url='/queue/recruit', params={ 'task': self.key() }, countdown=countdown)

    def _assign(self, worker):
        if self.assigned_to:
            return False
        self.assigned_to = worker.user
        self.assigned = datetime.now()
        self.put()
        return True

    def assign(self, worker):
        if db.run_in_transaction(self._assign, worker):
            worker.task = self
            worker.put()
            taskqueue.add(url='/queue/notify', params={ 'task': self.key(), 'event': 'accepted' })
            return True
        return False

    def complete(self, worker):
        if self.completed or self.assigned_to != worker.user:
            return False
        self.completed = datetime.now()
        self.put()
        if worker.task and worker.task.key() == self.key():
            worker.task = None
            worker.put()
        taskqueue.add(url='/queue/notify', params={ 'task': self.key(), 'event': 'completed' })
        return True

class UniqueIdStringProperty(db.StringProperty):
    def default_value(self):
        return str(uuid.uuid4())

class Worker(db.Model):
    user = db.UserProperty(required=True)
    joined = db.DateTimeProperty(auto_now_add=True)
    api_key = UniqueIdStringProperty(required=True)
    pools = db.ListProperty(db.Key, required=True)
    task = db.ReferenceProperty(Task)
    next_contact = db.DateTimeProperty(required=True, auto_now_add=True)

    @staticmethod
    def create(user):
        worker = Worker(key_name=user.user_id(), user=user)
        worker.put()
        return worker

    @staticmethod
    def free_for(task):
        query = Worker.all().filter('task =', None)
        if task.pool:
            query.filter('pools =', task.pool.key())
        query.order('next_contact')
        cursor_key = 'free_cursor_%s' % task.key()
        cursor = memcache.get(cursor_key)
        if cursor:
            query.with_cursor(cursor)
        for worker in query:
            if worker.next_contact > datetime.now():
                logging.info("Cannot contact %s yet for task %s", worker.user.email(), task.key())
                return
            memcache.set(cursor_key, query.cursor())
            if worker.user == task.creator:
                continue
            yield worker
        memcache.delete(cursor_key)

    def join_pool(self, name):
        pool = Pool.get_by_key_name(name) if name else False
        if pool:
            if pool.key() not in self.pools:
                logging.info("Worker %s joining group %s", self.user.email(), pool.key().name())
                self.pools.append(pool.key())
                self.put()
            return True
        return False

    def contacted(self):
        self.next_contact = datetime.now() + timedelta(minutes=5)
        self.put()

    def contactable(self):
        self.next_contact = datetime.now() + timedelta(seconds=1)
        self.put()
