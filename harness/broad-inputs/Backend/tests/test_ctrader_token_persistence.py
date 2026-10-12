import os
import tempfile
import unittest
from unittest.mock import Mock, patch

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import ctrader_connector
from db import Base
from models import CTraderOAuthToken
from services import ctrader_token_service


class CTraderTokenPersistenceTests(unittest.TestCase):
    def setUp(self):
        handle = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        handle.close()
        self.path = handle.name
        self.engine = create_engine(
            f"sqlite:///{self.path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.secret_patch = patch.dict(
            os.environ,
            {"CTRADER_CLIENT_SECRET": "stable-test-client-secret"},
            clear=False,
        )
        self.secret_patch.start()

    def tearDown(self):
        self.secret_patch.stop()
        self.engine.dispose()
        os.unlink(self.path)

    def test_tokens_are_encrypted_and_survive_new_database_session(self):
        self.assertTrue(ctrader_token_service.save_tokens(
            "access-secret",
            "refresh-secret",
            session_factory=self.Session,
            updated_by="test",
        ))

        with self.Session() as session:
            row = session.get(CTraderOAuthToken, "ctrader")
            self.assertNotIn("access-secret", row.encrypted_access_token)
            self.assertNotIn("refresh-secret", row.encrypted_refresh_token)

        restarted_factory = sessionmaker(bind=self.engine)
        loaded = ctrader_token_service.load_tokens(
            session_factory=restarted_factory,
        )
        self.assertEqual(loaded["access_token"], "access-secret")
        self.assertEqual(loaded["refresh_token"], "refresh-secret")
        self.assertEqual(loaded["source"], "encrypted_database")

    def test_explicit_disconnect_storage_clear_removes_tokens(self):
        ctrader_token_service.save_tokens(
            "access-secret",
            "refresh-secret",
            session_factory=self.Session,
        )
        self.assertTrue(ctrader_token_service.clear_tokens(
            session_factory=self.Session,
        ))
        self.assertEqual(
            ctrader_token_service.load_tokens(session_factory=self.Session),
            {},
        )

    def test_persisted_tokens_override_stale_render_environment(self):
        previous = dict(ctrader_connector.CTRADER_TOKEN_HYDRATION)
        try:
            ctrader_connector.CTRADER_TOKEN_HYDRATION.update({
                "checked_at": 0.0,
                "loaded": False,
                "source": None,
            })
            with (
                patch.dict(os.environ, {
                    "CTRADER_ACCESS_TOKEN": "stale-access",
                    "CTRADER_REFRESH_TOKEN": "stale-refresh",
                }),
                patch.object(
                    ctrader_connector,
                    "load_durable_ctrader_tokens",
                    return_value={
                        "access_token": "durable-access",
                        "refresh_token": "durable-refresh",
                    },
                ),
            ):
                state = ctrader_connector.hydrate_ctrader_tokens_from_storage(
                    force=True,
                )
                self.assertEqual(
                    os.environ["CTRADER_ACCESS_TOKEN"], "durable-access"
                )
                self.assertEqual(
                    os.environ["CTRADER_REFRESH_TOKEN"], "durable-refresh"
                )
                self.assertTrue(state["loaded"])
        finally:
            ctrader_connector.CTRADER_TOKEN_HYDRATION.clear()
            ctrader_connector.CTRADER_TOKEN_HYDRATION.update(previous)

    def test_oauth_callback_persists_new_tokens(self):
        response = Mock()
        response.ok = True
        response.json.return_value = {
            "accessToken": "new-access",
            "refreshToken": "new-refresh",
        }
        with (
            patch.object(ctrader_connector.requests, "post", return_value=response),
            patch.object(
                ctrader_connector,
                "get_ctrader_redirect_uri",
                return_value="https://example.test/ctrader/callback",
            ),
            patch.object(
                ctrader_connector,
                "persist_ctrader_tokens",
                return_value=True,
            ) as persist,
            patch.object(ctrader_connector, "update_env_file_values"),
            patch.object(ctrader_connector, "clear_ctrader_connection_cache"),
            patch.dict(os.environ, {
                "CTRADER_CLIENT_ID": "client-id",
                "CTRADER_CLIENT_SECRET": "client-secret",
            }),
        ):
            result = ctrader_connector.exchange_ctrader_authorization_code("code")

        self.assertTrue(result["ok"])
        self.assertTrue(result["durable_token_saved"])
        persist.assert_called_once_with(
            "new-access", "new-refresh", updated_by="oauth_callback"
        )

    def test_rotated_refresh_token_is_persisted(self):
        response = Mock()
        response.ok = True
        response.status_code = 200
        response.json.return_value = {
            "accessToken": "rotated-access",
            "refreshToken": "rotated-refresh",
        }
        config = {
            "client_id": "client-id",
            "client_secret": "client-secret",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
        }
        with (
            patch.object(ctrader_connector.requests, "post", return_value=response),
            patch.object(
                ctrader_connector,
                "persist_ctrader_tokens",
                return_value=True,
            ) as persist,
            patch.object(ctrader_connector, "update_env_file_values"),
        ):
            refreshed = ctrader_connector.refresh_ctrader_access_token(config)

        self.assertEqual(refreshed["access_token"], "rotated-access")
        self.assertEqual(refreshed["refresh_token"], "rotated-refresh")
        persist.assert_called_once_with(
            "rotated-access", "rotated-refresh", updated_by="token_refresh"
        )

    def test_explicit_connector_disconnect_clears_durable_tokens(self):
        with (
            patch.object(
                ctrader_connector,
                "load_ctrader_account_settings",
                return_value={
                    **ctrader_connector.DEFAULT_CTRADER_ACCOUNT_SETTINGS,
                    "active_account_id": "47810571",
                },
            ),
            patch.object(ctrader_connector, "save_ctrader_account_settings"),
            patch.object(ctrader_connector, "update_env_file_values"),
            patch.object(ctrader_connector, "clear_ctrader_connection_cache"),
            patch.object(
                ctrader_connector,
                "clear_durable_ctrader_tokens",
                return_value=True,
            ) as clear_durable,
        ):
            ctrader_connector.clear_ctrader_tokens_and_accounts()

        clear_durable.assert_called_once_with()
        self.assertEqual(
            ctrader_connector.CTRADER_TOKEN_HYDRATION["source"],
            "explicit_disconnect",
        )


if __name__ == "__main__":
    unittest.main()
