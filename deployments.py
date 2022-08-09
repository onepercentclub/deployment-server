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
try:
    import config
    app.config.update(vars(config))
except ModuleNotFoundError: 
    pass

app.config['REPOS'] = {
    'onepercentclub/reef': 'site_frontend',
    'onepercentclub/bluebottle': 'site_backend'
}

git = sh.git

github = requests.Session()
github.headers.update({
    'Content-Type': 'application/json',
    'Authorization': 'token {}'.format(app.config['GITHUB_ACCESS_TOKEN'])
})


def make_celery(app):
    celery = Celery(app.import_name)
    celery.conf.update(app.config["CELERY_CONFIG"])

    class ContextTask(celery.Task):
        def __call__(self, *args, **kwargs):
            with app.app_context():
                return self.run(*args, **kwargs)

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
        environment = 'staging'
    elif ref.startswith('refs/heads/release/'):
        environment = 'testing'
    elif ref.startswith('refs/heads/demo/'):
        environment = 'demo'

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

    response.raise_for_status()


def update_deployment_status(state, payload):
    response = github.post(
        payload['deployment']['statuses_url'],
        json.dumps({
            'state': state,
            'environment': payload['deployment']['original_environment'],
            'description': payload['deployment']['description']
        })
    )
    print(response.text)
    response.raise_for_status()

@celery.task()
def deploy(payload):
    state = 'success'
    update_deployment_status('pending', payload)

    environment = payload['deployment']['environment']
    os.chdir(app.config['ANSIBLE_PATH'])

    target = app.config['REPOS'][payload['repository']['full_name']]
    try:
        git_result = git.pull(_cwd=app.config['ANSIBLE_PATH'])
        args = (
            "git_username={GIT_USERNAME} git_password={GIT_PASSWORD} "
            "git_org={GIT_ORG} auto_confirm=true").format(
            **app.config
        )

        if environment != 'staging' or target != 'site_backend':
            args += " commit_hash={commit_hash}".format(
                commit_hash=payload['deployment']['sha']
            )

        result = getattr(sh, 'env/bin/ansible-playbook')(
            '--skip-tags=vault',
            '-i',  'hosts/linode', '-l', environment, '-vvv', '{}.yml'.format(
                target
            ),
            '-e', args, _cwd=app.config['ANSIBLE_PATH']
        )

        description = 'Deployment succeeded'
        log = str(result.stdout)
    except sh.ErrorReturnCode as e:
        description = 'Deploy failed'
        state = 'error'
        log = str(e.stdout)
        print(log)

    update_deployment_status(state, payload)
