from types import SimpleNamespace

import onboarding


class _DummyResponse:
    def __init__(self, status_code=200, payload=None, text=''):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _user(token='token'):
    return SimpleNamespace(id=42, alpaca_access_token=token, alpaca_data_feed='iex')


def test_verify_alpaca_data_feed_defaults_to_iex(mocker):
    user = _user()
    mocker.patch('onboarding.db.session.commit')
    mocker.patch('onboarding.requests.get', return_value=_DummyResponse(payload={}))

    onboarding.verify_alpaca_data_feed(user)

    assert user.alpaca_data_feed == 'iex'


def test_verify_alpaca_data_feed_sets_sip_when_plan_indicates_entitlement(mocker):
    user = _user()
    mocker.patch('onboarding.db.session.commit')
    mocker.patch(
        'onboarding.requests.get',
        return_value=_DummyResponse(payload={'market_data_subscription': 'sip'}),
    )

    onboarding.verify_alpaca_data_feed(user)

    assert user.alpaca_data_feed == 'sip'


def test_verify_alpaca_data_feed_no_token_exits_early(mocker):
    user = _user(token='')
    mock_get = mocker.patch('onboarding.requests.get')
    mock_commit = mocker.patch('onboarding.db.session.commit')

    onboarding.verify_alpaca_data_feed(user)

    assert not mock_get.called
    assert not mock_commit.called
