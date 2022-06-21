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


client = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
client.conf.update(app.config)


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
        environment = 'guineapig'

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


@client.task()
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
            "git_username={git_username} git_password={git_password} "
            "git_org={git_org} auto_confirm=true").format(
            **app.config
        )

        if environment != 'staging' or target != 'site_backend':
            args += " commit_hash={commit_hash}".format(
                commit_hash=payload['deployment']['sha']
            )

        result = getattr(sh, './ansible-playbook')(
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

    response = github.post(
        payload['deployment']['statuses_url'],
        json.dumps(
            {'state': state, 'description': description[-139:]})
    )
    response.raise_for_status()


@client.task()
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
        app.configp['SLACK_WEBHOOK'],
        json.dumps(data),
        headers={'Content-Type': 'application/json'}
    )

    response.raise_for_status()
