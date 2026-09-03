from tests.integration.setup_harness import Wizard


def test_web_setup_requests_do_not_match_server_contract(tmp_path):
    wizard = Wizard(tmp_path)
    try:
        token = wizard.token()
        form_body = {
            "database_url": "postgresql://redacted.invalid/db",
            "master_key_b64": "REDACTED",
            "owner": {"account_id": "acct-owner", "display_name": "System Owner"},
            "integrations": {
                "mattermost": {"url": "http://mattermost.invalid", "bot_token": "REDACTED"},
                "storage": {"root": "/tmp/redacted"},
            },
        }
        headers = {"X-Setup-Token": token}

        preflight = wizard.client.post("/setup/preflight", json=form_body, headers=headers)
        diff = wizard.client.post("/setup/diff", json=form_body, headers=headers)
        bootstrap = wizard.client.post("/setup/bootstrap", json=form_body, headers=headers)

        print("UI-shaped preflight:", preflight.status_code, preflight.text)
        print("UI-shaped diff:", diff.status_code)
        print("UI-shaped bootstrap:", bootstrap.status_code)
        assert preflight.status_code == 200
        assert preflight.json()["ok"] is False
        assert "SETUP_INPUT_MISSING" in preflight.text
        assert diff.status_code == 405
        assert bootstrap.status_code == 422
    finally:
        wizard.close()
