from hashlib import sha256
import hmac
import json
import os
import threading

from dropbox import Dropbox
from dropbox.files import DeletedMetadata, FolderMetadata, WriteMode
from flask import Flask, Response, request
from markdown import markdown
import redis

redis_url = os.environ['REDISTOGO_URL']
redis_client = redis.from_url(redis_url)

# App key and secret from the App console (dropbox.com/developers/apps)
APP_KEY = os.environ['APP_KEY']
APP_SECRET = os.environ['APP_SECRET']

app = Flask(__name__)

# A random secret used by Flask to encrypt session data cookies
app.secret_key = os.environ['FLASK_SECRET_KEY']

@app.before_first_request
def hello():
    access_token = os.environ['ACC_TOKEN']
    account = Dropbox(access_token).users_get_current_account().account_id
    redis_client.hset('tokens', account, access_token)
    process_user(account)

def process_user(account):

    '''Call /files/list_folder for the given user ID and process any changes.'''

    # OAuth token for the user
    token = redis_client.hget('tokens', account).decode('utf-8')

    # cursor for the user (None the first time)
    cursor = redis_client.hget('cursors', account).decode('utf-8')
    dbx = Dropbox(token)
    has_more = True

    while has_more:
        if cursor is None:
            result = dbx.files_list_folder(path='')
        else:
            result = dbx.files_list_folder_continue(cursor)

        for entry in result.entries:
            # Ignore deleted files, folders, and non-markdown files
            if (isinstance(entry, DeletedMetadata) or
                isinstance(entry, FolderMetadata) or
                not entry.path_lower.endswith('.md')):
                continue

            # Convert to Markdown and store as <basename>.html
            _, resp = dbx.files_download(entry.path_lower)
            html = markdown(str(resp.content, "utf-8")).encode("utf-8")
            dbx.files_upload(html, entry.path_lower[:-3] + '.html', mode=WriteMode('overwrite'))

        # Update cursor
        cursor = result.cursor
        redis_client.hset('cursors', account, cursor)

        # Repeat only if there's more to do
        has_more = result.has_more

@app.route('/webhook', methods=['GET'])
def challenge():
    '''Respond to the webhook challenge (GET request) by echoing back the challenge parameter.'''

    resp = Response(request.args.get('challenge'))
    resp.headers['Content-Type'] = 'text/plain'
    resp.headers['X-Content-Type-Options'] = 'nosniff'

    return resp

@app.route('/webhook', methods=['POST'])
def webhook():
    '''Receive a list of changed user IDs from Dropbox and process each.'''

    # Make sure this is a valid request from Dropbox
    signature = request.headers.get('X-Dropbox-Signature').encode("utf-8")

    if not hmac.compare_digest(signature, hmac.new(APP_SECRET.encode('utf-8'), request.data, sha256).hexdigest().encode('utf-8')):
        abort(403)

    for account in json.loads(request.data.decode("utf-8"))['list_folder']['accounts']:
        # We need to respond quickly to the webhook request, so we do the
        # actual work in a separate thread. For more robustness, it's a
        # good idea to add the work to a reliable queue and process the queue
        # in a worker process.
        threading.Thread(target=process_user, args=(account,)).start()
    return ''

if __name__=='__main__':
    app.run()
