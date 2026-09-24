from unittest.mock import patch
from backend import db,providers
from test_workspace import client


def test_save_validates_and_records_failure_without_losing_key(client):
    with patch.object(providers,'validation_probe') as probe:
        r=client.put('/api/providers/tencent-ocr/key',json={'secret_id':'fake','secret_key':'test-only'})
        assert r.status_code==200 and r.json()['status']=='verified'
        assert probe.call_count==1
    with patch.object(providers,'validation_probe',side_effect=providers.ProviderError('腾讯 OCR 调用失败：AuthFailure.UnauthorizedOperation')):
        r=client.put('/api/providers/tencent-ocr/key',json={'secret_id':'fake2','secret_key':'test-only2'})
        assert r.json()['status']=='blocked_auth'
    status=next(x for x in client.get('/api/providers').json() if x['provider']=='tencent-ocr')
    assert 'UnauthorizedOperation' in status['validation_error']
    assert 'test-only' not in str(status)
    assert db.load(providers.provider_secret('tencent-ocr'))['secret_id']=='fake2'


def test_late_validation_cannot_verify_replaced_or_removed_key(client):
    def supersede(provider,secret):
        with db.connect() as c:
            c.execute("UPDATE provider_settings SET credential_ref=NULL,status='unconfigured' WHERE provider=?",(provider,))
    with patch.object(providers,'validation_probe',side_effect=supersede):
        result=client.put('/api/providers/redfox/key',json={'key':'test-key'})
    assert result.json()['status']=='superseded'
    assert client.get('/api/providers').json()[0]['status']=='unconfigured'


def test_ocr_probe_uses_synthetic_image_without_business_writes(client):
    with patch.object(providers,'tencent_request',return_value={'TextDetections':[{'DetectedText':'OCR TEST 1234'}]}) as call:
        providers.validation_probe('tencent-ocr','test-only')
    assert call.call_args.args[1].startswith(b'\x89PNG')
    assert client.get('/api/evidence').json()==[]
