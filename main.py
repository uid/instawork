import cgi
import logging
import zlib

from google.appengine.api import memcache
from google.appengine.api import users
from google.appengine.api import xmpp
from google.appengine.ext import webapp
from google.appengine.ext.webapp import template
from google.appengine.ext.webapp import util

from models import *
import strings

def render(handler, templatefile, task=None, vars={}):
    vars.update({
        'user': users.get_current_user(),
        'login_url': users.create_login_url(handler.request.uri),
        'logout_url': users.create_logout_url('/'),
        'task': task
    })
    vars.update(strings.__dict__)
    handler.response.out.write(template.render('templates/'+templatefile+'.html', vars))

def signup_phrase_for(email):
    idx = zlib.adler32(email)
    return strings.adjectives[ idx % len(strings.adjectives) ] + " " + strings.nouns[ idx % len(strings.nouns) ]

class MainHandler(webapp.RequestHandler):
    def get(self):
        user = users.get_current_user()
        worker = user and Worker.get_by_key_name(user.user_id())
        if worker:
            render(self, 'status', worker.task, vars={
                'open': Task.all().filter('creator =', user).filter('completed =', None),
                'done': Task.all().filter('creator =', user).filter('completed !=', None)
            })
        elif user:
            render(self, 'signup', vars={ 'signup_phrase': signup_phrase_for(user.email()) })
        else:
            render(self, 'index')

    def post(self):
        user = users.get_current_user()
        if user:
            xmpp.send_invite(user.email())
            memcache.add(user.email(), user, namespace='user_emails')
            self.get()
        else:
            render(self, 'error')

class XMPPHandler(webapp.RequestHandler):
    def post(self):
        message = xmpp.Message(self.request.POST)
        sender = message.sender.partition('/')[0]
        user = memcache.get(sender, namespace='user_emails')
        if user and message.body.lower() == signup_phrase_for(sender):
            Worker(key_name=user.user_id(), user=user).put()
            message.reply("Welcome to Instawork!")

class CreateHandler(webapp.RequestHandler):
    def get(self):
        url = self.request.url + '&user_id=' + users.get_current_user().user_id()
        render(self, 'api_helper', vars={ 'name': 'create_task', 'url': url })

    def post(self):
        creator = Worker.get_by_key_name(self.request.get('user_id')).user
        task = Task.create(self.request.params, creator)
        task.queue(0)

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
        if not task.assigned_to:
            render(self, 'job_preview', task)
        elif task.assigned_to != worker.user:
            render(self, 'job_taken', task)
        elif task.completed:
            render(self, 'job_review', task)
        else:
            render(self, 'job_busy', task)

    def post(self, key):
        worker = Worker.get_by_key_name(users.get_current_user().user_id())
        task = Task.get(key)
        if worker.task:
            render(self, 'job_busy', task)
        elif task.assign(worker):
            self.redirect(task.fullURL())
        else:
            render(self, 'job_taken', task)

class DoneHandler(webapp.RequestHandler):
    def get(self, key):
        render(self, 'api_helper', vars={ 'name': 'done', 'url': self.request.uri })

    def post(self, key):
        worker = Worker.get_by_key_name(users.get_current_user().user_id())
        task = Task.get(key)
        if task.complete(worker):
            worker.contactable()
            xmpp.send_message(task.creator.email(), 'Your Instawork job was completed: %s' % task.title)
            render(self, 'job_done', task);
        else:
            render(self, 'error')

def routes():
    return [('/', MainHandler),
            ('/_ah/xmpp/message/chat/', XMPPHandler),
            ('/api/create_task', CreateHandler),
            ('/queue/recruit', RecruitHandler),
            ('/go/(.*)', JobHandler),
            ('/done/(.*)', DoneHandler)]

def main():
    application = webapp.WSGIApplication(routes(), debug=True)
    util.run_wsgi_app(application)

if __name__ == '__main__':
    main()
