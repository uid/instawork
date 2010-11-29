import datetime
import os

from google.appengine.api import memcache
from google.appengine.ext import db
from google.appengine.api.labs import taskqueue

class Task(db.Model):
    title = db.StringProperty(required=True)
    description = db.TextProperty(required=True)
    url = db.LinkProperty(required=True)
    created = db.DateTimeProperty(auto_now_add=True)
    creator = db.UserProperty(required=True)
    assigned = db.DateTimeProperty()
    assigned_to = db.UserProperty()
    completed = db.DateTimeProperty()

    def fullURL(self):
        return '%s?submitURL=http://%s/done/%s' % (self.url, os.environ['HTTP_HOST'], self.key())

    @staticmethod
    def create(params, creator):
        task = Task(title=params['title'], description=params['description'], url=params['url'], creator=creator)
        task.put()
        return task

    def queue(self, countdown):
        taskqueue.add(url='/queue/recruit', params={ 'task': self.key() }, countdown=countdown)

    def _assign(self, worker):
        if self.assigned_to:
            return False
        self.assigned_to = worker.user
        self.assigned = datetime.datetime.now()
        self.put()
        return True

    def assign(self, worker):
        if db.run_in_transaction(self._assign, worker):
            worker.task = self
            worker.put()
            return True
        return False

    def complete(self, worker):
        if self.completed or self.assigned_to != worker.user:
            return False
        self.completed = datetime.datetime.now()
        self.put()
        if worker.task and worker.task.key() == self.key():
            worker.task = None
            worker.put()
        return True

class Worker(db.Model):
    user = db.UserProperty(required=True)
    joined = db.DateTimeProperty(auto_now_add=True)
    task = db.ReferenceProperty(Task)

    @staticmethod
    def free():
        query = lambda: Worker.all().filter('task =', None)
        worker_cursor = memcache.get('worker_cursor')
        workers = query()
        if worker_cursor:
            workers = workers.with_cursor(worker_cursor)
        try:
            worker = workers.fetch(1)[0]
        except IndexError:
            workers = query()
            worker = workers.fetch(1)[0]
        memcache.set('worker_cursor', workers.cursor())
        return worker

