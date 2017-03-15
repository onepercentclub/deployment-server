import os
import json
import hashlib
import hmac
import time
import os
import subprocess

from flask import Flask, request
import sh
import requests

app = Flask('deployment_server')
app.config['GITHUB_WEBHOOK_SECRET'] = os.environ.get('GITHUB_WEBHOOK_SECRET')
app.config['GITHUB_ACCESS_TOKEN'] = os.environ.get('GITHUB_ACCESS_TOKEN')
app.config['ANSIBLE_PATH'] = os.environ.get('ANSIBLE_PATH')
app.config['REPOS'] = {
    'onepercentclub/reef': 'site_frontend',
    'onepercentclub/bluebottle': 'site_backend'
}

git = sh.git
ansible = getattr(sh, 'ansible-playbook')


github = requests.Session()
github.headers.update({
    'Content-Type': 'application/json',
    'Authorization': 'token {}'.format(app.config['GITHUB_ACCESS_TOKEN'])
})


@app.route('/webhook/', methods=['GET', 'POST'])
def webhook():
    if request.method == 'POST':
        event = request.headers['X-GitHub-Event']
        signature = request.headers['X-Hub-Signature']

        mac = hmac.new(app.config['GITHUB_WEBHOOK_SECRET'], msg=request.data, digestmod=hashlib.sha1)
        if not 'sha1={}'.format(mac.hexdigest()) == signature:
            return 'Invalid signature', 403

        print event
        if event == 'ping':
            return json.dumps({'msg': 'hi'})
        if event == 'deployment':
            deploy()
        if event == 'push':
            create_deployment()

        return '', 201


def create_deployment():
    payload = request.json
    ref = payload['ref']

    environment = None
    if ref == 'refs/heads/master':
        environment = 'staging'
    elif ref.startswith('refs/heads/release/'):
        environment = 'testing'
    elif ref == 'refs/heads/develop' or True:
        environment = 'development'

    if not environment:
        return

    deployment = {
        'ref': ref,
        'environment': environment,
        'auto_merge': False,
        'description': payload['head_commit']['message']
    }
    response = github.post(
        payload['repository']['deployments_url'],
        json.dumps(deployment)
    )

    response.raise_for_status()


def deploy():
    payload = request.json
    state = 'success'
    response = github.post(
        payload['deployment']['statuses_url'], json.dumps({'state': 'pending'})
    )
    environment = payload['deployment']['environment']
    os.chdir(app.config['ANSIBLE_PATH'])

    target = app.config['REPOS'][payload['repository']['full_name']]
    try:
        git_result = git.pull(_cwd=app.config['ANSIBLE_PATH'])
        result = ansible(
            '--vault-password-file=/dev/null/', '--skip-tags=vault', '--check',
            '-i',  'hosts/linode', '-l', environment, '{}.yml'.format(target),
            '-e', "commit_hash={}".format(payload['deployment']['sha']),
            _cwd=app.config['ANSIBLE_PATH']
        )
    except Exception as e:
        print e
        state = 'error'

    response = github.post(
        payload['deployment']['statuses_url'], json.dumps({'state': state})
    )
    response.raise_for_status()
