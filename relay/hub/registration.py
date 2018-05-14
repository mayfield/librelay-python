import logging
import secrets
from .. import protobufs
from .. import storage
from ..provisioning_cipher import ProvisioningCipher
from ..websocket_resource import WebSocketResource
from .atlas import AtlasClient
from .signal import SignalClient
from axolotl.util import KeyHelper

logger = logging.getLoger(__name__)
default_name = 'librelay'


class ReturnInterface(object):
    pass


def generate_password():
    return secrets.token_urlsafe(16)


def generate_signaling_key():
    return secrets.token_bytes(32 + 20)


async def register_account(atlas_client=None, name=default_name):
    if atlas_client is None:
        atlas_client = await AtlasClient.factory()
    registration_id = KeyHelper.generateRegistrationId()
    password = generate_password()
    signalingKey = generate_signaling_key()
    response = await atlas_client.fetch('/v1/provision/account', {
        method: 'PUT',
        json: {
            "signalingKey": signalingKey.toString('base64'),
            "supportsSms": False,
            "fetchesMessages": True,
            "registrationId": registration_id,
            "name": name,
            "password": password
        }
    })
    addr = response.userId
    username = f'{addr}.{response.deviceId}'
    identity = KeyHelper.generateIdentityKeyPair()
    await storage.clear_session_store()
    await storage.remove_our_identity()
    await storage.remove_identity(addr)
    await storage.save_identity(addr, identity.pubKey)
    await storage.save_our_identity(identity)
    await storage.put_state('addr', addr)
    await storage.put_state('serverUrl', response.serverUrl)
    await storage.put_state('deviceId', response.deviceId)
    await storage.put_state('name', name)
    await storage.put_state('username', username)
    await storage.put_state('password', password)
    await storage.put_state('registration_id', registration_id)
    await storage.put_state('signalingKey', signalingKey)
    sc = SignalClient(username, password, response.serverUrl)
    await sc.registerKeys(await sc.generateKeys())


async def register_device(atlas_client=None, name=default_name,
                          auth_provision=True, on_provision_ready=None):
    if atlas_client is None:
        atlas_client = await AtlasClient.factory()
    account_info = await atlas_client.fetch('/v1/provision/account')
    if not account_info.devices:
        logger.error("Must use `registerAccount` for first device")
        raise TypeError("No Account")
    signalClient = SignalClient(null, null, account_info.serverUrl)
    if not on_provision_rReady and autoProvision:
        raise TypeError("Missing: onProvisionReady callback")
    returnInterface = ReturnInterface()
    returnInterface.waiting = True
    provisioningCipher = ProvisioningCipher()
    pubKey = provisioningCipher.getPublicKey().toString('base64')
    webSocketWaiter = Promise((resolve, reject) => {
        wsr = WebSocketResource(signalClient.getProvisioningWebSocketURL(), {
            keepalive: {path: '/v1/keepalive/provisioning'},
            handleRequest: request => {
                if (request.path == "/v1/address" and request.verb == "PUT") {
                    proto = protobufs.ProvisioningUuid.decode(request.body)
                    request.respond(200, 'OK')
                    if (autoProvision) {
                        atlas_client.fetch('/v1/provision/request', {
                            method: 'POST',
                            json: {
                                uuid: proto.uuid,
                                key: pubKey
                            }
                        }).catch(reject)
                    }
                    if (options.onProvisionReady) {
                        r = options.onProvisionReady(proto.uuid, pubKey)
                        if (r instanceof Promise) {
                            r.catch(reject)
                        }
                    }
                } else if (request.path == "/v1/message" and request.verb == "PUT") {
                    msgEnvelope = protobufs.ProvisionEnvelope.decode(request.body)
                    request.respond(200, 'OK')
                    wsr.close()
                    resolve(msgEnvelope)
                } else {
                    reject(Exception('Unknown websocket message ' + request.path))
                }
            }
        })
    })
    await wsr.connect()

    async def _done():
        provisionMessage = await provisioningCipher.decrypt(await webSocketWaiter)
        returnInterface.waiting = False
        addr = provisionMessage.addr
        identity = provisionMessage.identityKeyPair
        if (provisionMessage.addr != account_info.userId) {
            raise Exception('Security Violation: Foreign account sent us an identity key!')
        }
        registration_id = KeyHelper.generateRegistrationId()
        password = generate_password()
        signalingKey = generate_signaling_key()
        response = await signalClient.request({
            httpType: 'PUT',
            call: 'devices',
            urlParameters: '/' + provisionMessage.provisioningCode,
            jsonData: {
                signalingKey: signalingKey.toString('base64'),
                supportsSms: False,
                fetchesMessages: True,
                registration_id,
                name
            },
            username: addr,
            password,
            validateResponse: {deviceId: 'number'}
        })
        username = f'{addr}.{response.deviceId}'
        await storage.clearSessionStore()
        await storage.remove_our_identity()
        await storage.remove_identity(addr)
        await storage.save_identity(addr, identity.pubKey)
        await storage.save_our_identity(identity)
        await storage.put_state('addr', addr)
        await storage.put_state('serverUrl', signalClient.url)
        await storage.put_state('deviceId', response.deviceId)
        await storage.put_state('name', name)
        await storage.put_state('username', username)
        await storage.put_state('password', password)
        await storage.put_state('registration_id', registration_id)
        await storage.put_state('signalingKey', signalingKey)
        authedClient = SignalClient(username, password, signalClient.url)
        await authedClient.registerKeys(await authedClient.generateKeys())
    done = _done()

    async def cancel():
        wsr.close()
        try:
            await webSocketWaiter
        except Exception as e:
            logger.warn("Ignoring web socket error: " + e)
    returnInterface.cancel = cancel

    return returnInterface
