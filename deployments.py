import os
import json
import hashlib
import hmac
import time
import os
import subprocess

from celery import Celery
from flask import Flask, request, url_for
import sh
import requests


app = Flask('deployment_server')
app.config['GITHUB_WEBHOOK_SECRET'] = os.environ['GITHUB_WEBHOOK_SECRET']
app.config['GITHUB_ACCESS_TOKEN'] = os.environ['GITHUB_ACCESS_TOKEN']
app.config['ANSIBLE_PATH'] = os.environ['ANSIBLE_PATH']
app.config['REPOS'] = {
    'onepercentclub/reef': 'site_frontend',
    'onepercentclub/bluebottle': 'site_backend'
}
app.config['CELERY_BROKER_URL'] = os.environ['REDIS_URL']
app.config['CELERY_RESULT_BACKEND'] = os.environ['REDIS_URL']
app.config['SERVER_NAME'] = 'deployments.dokku.onepercentclub.com'
app.config['JIRIT'] = {
    'jira_email': os.environ['JIRA_EMAIL'],
    'jira_password': os.environ['JIRA_PASSWORD'],
    'jira_url': os.environ['JIRA_URL'],
    'jira_id': os.environ['JIRA_ID'],
    'git_username': os.environ['GIT_USERNAME'],
    'git_password': os.environ['GIT_PASSWORD'],
    'git_org': os.environ['GIT_ORG'],
}


git = sh.git
ansible = getattr(sh, 'ansible-playbook')
slack_webhook = os.environ['SLACK_WEBHOOK']

github = requests.Session()
github.headers.update({
    'Content-Type': 'application/json',
    'Authorization': 'token {}'.format(app.config['GITHUB_ACCESS_TOKEN'])
})


def make_celery(app):
    celery = Celery(app.import_name, backend=app.config['CELERY_RESULT_BACKEND'],
                    broker=app.config['CELERY_BROKER_URL'])
    celery.conf.update(app.config)

    TaskBase = celery.Task

    class ContextTask(TaskBase):
        abstract = True

        def __call__(self, *args, **kwargs):
            with app.app_context():
                return TaskBase.__call__(self, *args, **kwargs)
    celery.Task = ContextTask
    return celery


celery = make_celery(app)


@app.route('/webhook/', methods=['GET', 'POST'])
def webhook():
    if request.method == 'POST':
        event = request.headers['X-GitHub-Event']
        signature = request.headers['X-Hub-Signature']
        payload = request.json

        mac = hmac.new(app.config['GITHUB_WEBHOOK_SECRET'],
                       msg=request.data, digestmod=hashlib.sha1)
        if not 'sha1={}'.format(mac.hexdigest()) == signature:
            return 'Invalid signature', 403

        if event == 'ping':
            return json.dumps({'msg': 'hi'})
        if event == 'deployment':
            deploy.delay(payload)
        if event == 'push':
            create_deployment.delay(payload)

        return '', 201


@celery.task()
def create_deployment(payload):
    ref = payload['ref']

    environment = None
    if ref == 'refs/heads/master':
        environment = 's.goodup.com'
    elif ref.startswith('refs/heads/release/'):
        environment = 't.goodup.com'
    elif ref.startswith('refs/heads/release-2/'):
        environment = 't2.goodup.com'
    elif ref.startswith('refs/heads/release-3/'):
        environment = 't3.goodup.com'

    if not environment:
        return

    deployment = {
        'ref': ref,
        'environment': environment,
        'auto_merge': False,
        'required_contexts': [],
        'description': payload['head_commit']['message']
    }
    response = github.post(
        payload['repository']['deployments_url'],
        json.dumps(deployment)
    )

    print response.content
    response.raise_for_status()


@celery.task()
def deploy(payload):
    state = 'success'
    response = github.post(
        payload['deployment']['statuses_url'],
        json.dumps({
            'state': 'pending',
            'description': 'Started deploy'
        })
    )
    response.raise_for_status()

    environment = payload['deployment']['environment']
    os.chdir(app.config['ANSIBLE_PATH'])

    target = app.config['REPOS'][payload['repository']['full_name']]
    try:
        git_result = git.pull(_cwd=app.config['ANSIBLE_PATH'])
        args = (
            "jira_email={jira_email} jira_password={jira_password} jira_url={jira_url} "
            "jira_id={jira_id} git_username={git_username} git_password={git_password} "
            "git_org={git_org} auto_confirm=true").format(
            **app.config['JIRIT']
        )

        if environment != 'staging' or target != 'site_backend':
            args += " commit_hash={commit_hash}".format(
                commit_hash=payload['deployment']['sha'])

        result = ansible(
            '--vault-password-file=/dev/null/', '--skip-tags=vault',
            '-i',  'hosts/hosts.yml', '-l', environment, '-vvv', '{}.yml'.format(
                target),
            '-e', args, _cwd=app.config['ANSIBLE_PATH']
        )
        print result, '!!!!'

        description = 'Deployment succeeded'
        log = str(result.stdout)
    except sh.ErrorReturnCode as e:
        print e, e.stdout
        description = 'Deploy failed'
        state = 'error'
        log = str(e.stdout)

    response = github.post(
        payload['deployment']['statuses_url'],
        json.dumps(
            {'state': state, 'description': description[-139:]})
    )
    response.raise_for_status()


@celery.task()
def send_slack_message(payload):
    state = payload['deployment_status']['state']
    description = payload['deployment_status']['description']
    environment = payload['deployment']['environment']
    deployment = payload['deployment']['description']

    color_map = {
        'pending': 'warning',
        'error': 'danger',
        'success': 'good'
    }

    color = color_map[state]

    data = {
        'channel': '#test-deploys',
        'attachments': [{
            "title": 'Deploying {} to {}'.format(deployment, environment),
            "text": description,
            "color": color,
            "title_link": url_for(
                'deployment',
                user=payload['repository']['full_name'].split('/')[0],
                repo=payload['repository']['name'],
                id=payload['deployment']['id'],
                _external=True
            )
        }]
    }

    response = requests.post(
        slack_webhook,
        json.dumps(data),
        headers={'Content-Type': 'application/json'}
    )

    response.raise_for_status()
