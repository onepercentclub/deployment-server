import os
import json
import hashlib
import hmac
import time
import os
import subprocess

from flask import Flask, request, url_for
from flask_redis import FlaskRedis
import sh
import requests


app = Flask('deployment_server')
app.config['GITHUB_WEBHOOK_SECRET'] = os.environ['GITHUB_WEBHOOK_SECRET']
app.config['GITHUB_ACCESS_TOKEN'] = os.environ['GITHUB_ACCESS_TOKEN']
app.config['ANSIBLE_PATH'] = os.environ['ANSIBLE_PATH']
app.config['REPOS'] = {
    'eodolphi/test-repo': 'site_frontend',
    'onepercent/reef': 'site_frontend',
    'onepercentclub/bluebottle': 'site_backend'
}
app.config['REDIS_URL'] = os.environ['REDIS_URL']

redis_store = FlaskRedis(app)


git = sh.git
ansible = getattr(sh, 'ansible-playbook')
slack_webhook = os.environ['SLACK_WEBHOOK']

github = requests.Session()
github.headers.update({
    'Content-Type': 'application/json',
    'Authorization': 'token {}'.format(app.config['GITHUB_ACCESS_TOKEN'])
})


@app.route('/deployment/<user>/<repo>/<id>')
def deployment(user, repo, id):
    response = github.get(
        'https://api.github.com/repos/{user}/{repo}/deployments/{id}/statuses'.format(
            user=user, repo=repo, id=id
        )
    )

    return redis_store.get(
        'deployment-{}/{}-{}'.format(
            user, repo, id
        )
    )
    return response.content, 200


@app.route('/webhook/', methods=['GET', 'POST'])
def webhook():
    if request.method == 'POST':
        event = request.headers['X-GitHub-Event']
        signature = request.headers['X-Hub-Signature']

        mac = hmac.new(app.config['GITHUB_WEBHOOK_SECRET'], msg=request.data, digestmod=hashlib.sha1)
        if not 'sha1={}'.format(mac.hexdigest()) == signature:
            return 'Invalid signature', 403

        if event == 'ping':
            return json.dumps({'msg': 'hi'})
        if event == 'deployment':
            deploy()
        if event == 'deployment_status':
            send_slack_message()
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
    elif ref == 'refs/heads/develop':
        environment = 'development'
    elif ref == 'refs/heads/test/temp':
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
        payload['deployment']['statuses_url'],
        json.dumps({
            'state': 'pending',
            'description': 'Started deploy'
        })
    )
    environment = payload['deployment']['environment']
    os.chdir(app.config['ANSIBLE_PATH'])

    target = app.config['REPOS'][payload['repository']['full_name']]
    try:
        git_result = git.pull(_cwd=app.config['ANSIBLE_PATH'])
        result = ansible(
            '--vault-password-file=/dev/null/', '--skip-tags=vault',
            '-i',  'hosts/linode', '-l', environment, '{}.yml'.format(target),
            '-e', "commit_hash={}".format(payload['deployment']['sha']),
            _cwd=app.config['ANSIBLE_PATH']
        )

        description = 'Deployment succeeded'
        log = str(result.stdout)
    except sh.ErrorReturnCode as e:
        description = 'Deploy failed'
        state = 'error'
        log = str(e.stdout)

    redis_store.set(
        'deployment-{}-{}'.format(
            payload['repository']['full_name'],
            payload['deployment']['id']
        ),
        str(log)
    )

    target_url = url_for(
        'deployment',
        user=payload['repository']['full_name'].split('/')[0],
        repo=payload['repository']['name'],
        id=payload['deployment']['id'],
        _external=True
    )
    response = github.post(
        payload['deployment']['statuses_url'],
        json.dumps({'state': state, 'description': description[-139:], 'target_url': target_url})
    )
    response.raise_for_status()


def send_slack_message():
    payload = request.json
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
