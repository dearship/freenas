# Copyright (c) 2015 iXsystems, Inc.
# All rights reserved.
# This file is a part of TrueNAS
# and may not be copied and/or distributed
# without the express permission of iXsystems.
from middlewared.client import Client, ClientException, CallTimeout
from middlewared.schema import accepts, Any, Bool, Dict, Int, List, Str
from middlewared.service import CallError, Service, job, private
from middlewared.utils import start_daemon_thread
from middlewared.utils.osc import set_thread_name

import errno
import json
import logging
import requests
import socket
import threading
import time

REMOTE_CLIENT = None
logger = logging.getLogger('failover.remote')


class RemoteClient(object):

    def __init__(self, remote_ip):
        self.remote_ip = remote_ip
        self.client = None
        self.connected = threading.Event()

    def run(self):
        set_thread_name('ha_connection')
        refused = False
        while True:
            try:
                self.connect_and_wait()
                refused = False
            except ConnectionRefusedError:
                if not refused:
                    logger.error('Persistent connection refused, retrying every 5 seconds')
                refused = True
            except Exception:
                logger.error('Remote connection failed', exc_info=True)
                refused = False
            time.sleep(5)

    def connect_and_wait(self):
        # 860 is the iSCSI port and blocked by the failover script
        try:
            with Client(
                f'ws://{self.remote_ip}:6000/websocket',
                reserved_ports=True, reserved_ports_blacklist=[860],
            ) as c:
                self.client = c
                self.connected.set()
                c._closed.wait()
        except OSError as e:
            if e.errno in (
                errno.EPIPE,  # Happens when failover is configured on cxl device that has no link
                errno.ENETDOWN, errno.EHOSTDOWN, errno.ENETUNREACH, errno.EHOSTUNREACH,
                errno.ECONNREFUSED,
            ) or isinstance(e, socket.timeout):
                raise ConnectionRefusedError()
            raise
        finally:
            self.client = None
            self.connected.clear()

    def call(self, *args, **kwargs):
        try:
            if not self.connected.wait(timeout=20):
                raise CallError('Remote connection unavailable', errno.ECONNREFUSED)
            return self.client.call(*args, **kwargs)
        except AttributeError as e:
            # ws4py traceback which can happen when connection is lost
            if "'NoneType' object has no attribute 'text_message'" in str(e):
                raise CallError('Remote connection closed.', errno.EREMOTE)
            else:
                raise
        except ClientException as e:
            raise CallError(str(e), e.errno or errno.EFAULT)

    def sendfile(self, token, local_path, remote_path):
        r = requests.post(
            f'http://{self.remote_ip}:6000/_upload/',
            files=[
                ('data', json.dumps({
                    'method': 'filesystem.put',
                    'params': [remote_path],
                })),
                ('file', open(local_path, 'rb')),
            ],
            headers={
                'Authorization': f'Token {token}',
            },
        )
        job_id = r.json()['job_id']
        # TODO: use event subscription in the client instead of polling
        while True:
            rjob = self.client.call('core.get_jobs', [('id', '=', job_id)])
            if rjob:
                rjob = rjob[0]
                if rjob['state'] == 'FAILED':
                    raise CallError(
                        f'Failed to send {local_path} to Standby Controller: {job["error"]}.'
                    )
                elif rjob['state'] == 'ABORTED':
                    raise CallError(
                        f'Failed to send {local_path} to Standby Controller, job aborted by user.'
                    )
                elif rjob['state'] == 'SUCCESS':
                    break
            time.sleep(0.5)


class FailoverService(Service):

    @private
    async def remote_ip(self):
        node = await self.middleware.call('failover.node')
        if node == 'A':
            remote = '169.254.10.2'
        elif node == 'B':
            remote = '169.254.10.1'
        else:
            raise CallError(f'Node {node} invalid for call_remote', errno.EBADRPC)
        return remote

    @accepts(
        Str('method'),
        List('args', default=[]),
        Dict(
            'options',
            Int('timeout'),
            Bool('job', default=False),
            Bool('job_return', default=None, null=True),
            Any('callback'),
        ),
    )
    def call_remote(self, method, args, options=None):
        options = options or {}
        job_return = options.get('job_return')
        if job_return is not None:
            options['job'] = 'RETURN'
        try:
            return REMOTE_CLIENT.call(method, *args, **options)
        except CallTimeout:
            raise CallError('Call timeout', errno.ETIMEDOUT)

    @private
    def sendfile(self, token, src, dst):
        REMOTE_CLIENT.sendfile(token, src, dst)

    @private
    async def ensure_remote_client(self):
        global REMOTE_CLIENT
        if REMOTE_CLIENT is not None:
            return
        try:
            remote_ip = await self.middleware.call('failover.remote_ip')
            REMOTE_CLIENT = RemoteClient(remote_ip)
            start_daemon_thread(target=REMOTE_CLIENT.run)
        except CallError as e:
            pass


async def setup(middleware):
    if await middleware.call('failover.licensed'):
        await middleware.call('failover.ensure_remote_client')
