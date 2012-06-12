import cgi
import logging
import zlib

from google.appengine.dist import use_library
use_library('django', '1.2')

from django.utils import simplejson

from google.appengine.api import app_identity
from google.appengine.api import channel
from google.appengine.api import memcache
from google.appengine.api import urlfetch
from google.appengine.api import users
from google.appengine.api import xmpp
from google.appengine.ext import blobstore
from google.appengine.ext import webapp
from google.appengine.ext.webapp import blobstore_handlers
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp import util

from models import *
import strings
from utils import *

def render(handler, templatefile, vars={}):
    vars.update({
        'user': users.get_current_user(),
        'login_url': users.create_login_url(handler.request.uri),
        'logout_url': users.create_logout_url('/')
    })
    vars.update(strings.__dict__)
    handler.response.out.write(template.render('templates/'+templatefile+'.html', vars))

def error(handler, code, message=None):
    handler.response.clear()
    handler.response.set_status(code, message)
    render(handler, 'error', {
        'code': code,
        'message': message,
        'default': webapp.Response.http_status_message(code)
    })

def json(handler, dict):
    handler.response.out.write(simplejson.dumps(dict))

def signup_phrase_for(email):
    idx = zlib.adler32(email)
    return strings.adjectives[ idx % len(strings.adjectives) ] + " " + strings.nouns[ idx % len(strings.nouns) ]

class MainHandler(webapp.RequestHandler):
    def get(self):
        user = users.get_current_user()
        worker = user and Worker.get_by_key_name(user.user_id())
        if worker:
            render(self, 'status', {
                'worker': worker,
                'open': Task.all().filter('creator =', user).filter('completed =', None),
                'done': Task.all().filter('creator =', user).filter('completed !=', None)
            })
        elif user:
            render(self, 'signup', {
                'app_jid': '%s@appspot.com' % app_identity.get_application_id(),
                'signup_phrase': signup_phrase_for(user.email().lower()),
                'channel_token': channel.create_channel(user.user_id() + 'signup')
            })
        else:
            render(self, 'index')

    def post(self):
        user = users.get_current_user()
        worker = user and Worker.get_by_key_name(user.user_id())
        if worker:
            pool = self.request.get('pool')
            if worker.join_pool(pool) or not pool:
                self.redirect('/')
            else:
                error(self, 404, 'Unable to Join Group')
        elif user:
            logging.info("Sending invite to %s", user.email())
            xmpp.send_invite(user.email())
            memcache.add(user.email().lower(), user, namespace='user_emails')
            self.response.set_status(204)
        else:
            self.response.set_status(403)

class JabberChatHandler(webapp.RequestHandler):
    def post(self):
        message = xmpp.Message(self.request.POST)
        sender = message.sender.partition('/')[0].lower()
        user = memcache.get(sender, namespace='user_emails')
        if user and message.body.lower() == signup_phrase_for(sender):
            Worker.create(user)
            message.reply("Welcome to Instawork!")
            channel.send_message(user.user_id() + 'signup', 'confirmed')
        else:
            logging.warn("Signup failed for %s (%s) \"%s\"", sender, user, message.body)

class JabberErrorHandler(webapp.RequestHandler):
    def post(self):
        sender = self.request.get('from')
        stanza = self.request.get('stanza')
        logging.error('XMPP error from %s: %s', sender, stanza)

class RequesterHandler(webapp.RequestHandler):
    def get(self):
        worker = Worker.get_by_key_name(users.get_current_user().user_id())
        render(self, 'requester', { 'api_key': worker.api_key })

class TaskCreateHandler(webapp.RequestHandler):
    def get(self):
        user_id = users.get_current_user().user_id()
        worker = Worker.get_by_key_name(user_id)
        url = url_with_params(self.request.url, {
            'userId': user_id,
            'secretKey': worker.api_key
        })
        render(self, 'api_helper', { 'name': 'create_task', 'url': url })

    def post(self):
        creator = Worker.get_by_key_name(self.request.get('userId'))
        if creator.api_key != self.request.get('secretKey'):
            error(self, 403)
            return
        task = Task.create(self.request.params, creator.user)
        task.queue(0)
        json(self, task.to_dict())

class TaskGetHandler(webapp.RequestHandler):
    def get(self):
        creator = Worker.get_by_key_name(self.request.get('userId'))
        if creator.api_key != self.request.get('secretKey'):
            error(self, 403)
            return
        if self.request.get('taskId'):
            task = Task.get(self.request.get('taskId'))
            if task:
                json(self, task.to_dict())
            else:
                error(self, 404)
        else:
            tasks = Task.all().filter('creator =', creator.user)
            json(self, { 'tasks': [ task.to_dict() for task in tasks ] })

class PoolCreateHandler(webapp.RequestHandler):
    def get(self):
        user_id = users.get_current_user().user_id()
        worker = Worker.get_by_key_name(user_id)
        url = url_with_params(self.request.url, {
            'userId': user_id,
            'secretKey': worker.api_key
        })
        render(self, 'api_helper', { 'name': 'create_pool', 'url': url })

    def post(self):
        creator = Worker.get_by_key_name(self.request.get('userId'))
        if creator.api_key != self.request.get('secretKey'):
            error(self, 403)
            return
        pool = Pool.create(self.request.params, creator.user)
        json(self, pool.to_dict())

class NotifyHandler(webapp.RequestHandler):
    def post(self):
        task = Task.get(self.request.get('task'))
        if not task:
            logging.warn("Notification aborted for missing task %s", self.request.get('task'))
            return
        if not task.notify_url:
            return
        try:
            url = url_with_params(task.notify_url, {
                'taskId': task.key(),
                'event': self.request.get('event')
            })
            urlfetch.fetch(url)
        except urlfetch.Error, err: # XXX better syntax in Python 2.6
            logging.error("Notification for task %s on %s error %s", task.key(), url, err)

class RecruitHandler(webapp.RequestHandler):
    def post(self):
        task = Task.get(self.request.get('task'))
        if not task:
            logging.warn("Recruitment aborted for missing task %s", self.request.get('task'))
            return
        if task.assigned_to:
            return
        for worker in Worker.free_for(task):
            if xmpp.get_presence(worker.user.email()):
                logging.info("Offering to %s task %s", worker.user.email(), task.key())
                worker.contacted()
                xmpp.send_message(worker.user.email(),
                                  template.render('templates/job_offer.xml', { 'host': self.request.host, 'task': task }),
                                  raw_xml=True)
                task.queue(30)
                return
        logging.warn("No free workers for task %s", task.key())
        task.queue(15)

class JobHandler(webapp.RequestHandler):
    def get(self, key):
        worker = Worker.get_by_key_name(users.get_current_user().user_id())
        task = Task.get(key)
        if not task:
            error(self, 404, 'Task not found')
        elif not task.assigned_to:
            render(self, 'job_preview', { 'task': task, 'own': task.creator == worker.user })
        elif task.assigned_to != worker.user:
            render(self, 'job_taken', { 'task': task })
        elif task.completed:
            render(self, 'job_review', { 'task': task })
        else:
            render(self, 'job_busy', { 'task': task })

    def post(self, key):
        worker = Worker.get_by_key_name(users.get_current_user().user_id())
        task = Task.get(key)
        if worker.task:
            render(self, 'job_busy', { 'task': worker.task })
        elif task.assign(worker):
            self.redirect(task.fullURL())
        else:
            render(self, 'job_taken', { 'task': task })

class DoneHandler(webapp.RequestHandler):
    def get(self, key):
        render(self, 'api_helper', { 'name': 'done', 'url': self.request.uri })

    def post(self, key):
        worker = Worker.get_by_key_name(users.get_current_user().user_id())
        task = Task.get(key)
        if task.complete(worker):
            worker.contactable()
            render(self, 'job_done', { 'task': task });
        else:
            error(self, 500)

class AdminStatusHandler(webapp.RequestHandler):
    def get(self):
        render(self, 'admin_status', {
            'active': Task.all().filter('assigned !=', None).filter('completed =', None),
            'queued': Task.all().filter('assigned =', None),
            'total': Task.all().count(),
            'busy': Worker.all().filter('task !=', None),
            'free': Worker.all().filter('task =', None).order('next_contact')
        })

class AdminExportHandler(webapp.RequestHandler):
    def get(self):
        tasks = Task.all()
        json(self, {
            'tasks': [ task.to_dict() for task in tasks ]
        })

class AdminBlobHandler(blobstore_handlers.BlobstoreDownloadHandler):
  def get(self, resource):
    resource = str(urllib.unquote(resource))
    blob_info = blobstore.BlobInfo.get(resource)
    self.send_blob(blob_info)

def routes():
    return [('/', MainHandler),
            ('/_ah/xmpp/message/chat/', JabberChatHandler),
            ('/_ah/xmpp/message/error/', JabberErrorHandler),
            ('/requester', RequesterHandler),
            ('/api/create_task', TaskCreateHandler),
            ('/api/get_tasks?', TaskGetHandler),
            ('/api/create_pool', PoolCreateHandler),
            ('/queue/notify', NotifyHandler),
            ('/queue/recruit', RecruitHandler),
            ('/go/(.*)', JobHandler),
            ('/done/(.*)', DoneHandler),
            ('/admin/status', AdminStatusHandler),
            ('/admin/export', AdminExportHandler),
            ('/admin/blob/(.*)', AdminBlobHandler)]

def main():
    application = webapp.WSGIApplication(routes(), debug=True)
    util.run_wsgi_app(application)

if __name__ == '__main__':
    main()
