import pytest

from brain.config import Config

ROLES = {"lead": "Leads and integrates.", "builder": "Builds.", "reviewer": "Reviews, read-only.",
         "tester": "Tests.", "planner": "Plans."}


def make_config(root="."):
    return Config(root, {"roles": ROLES, "sections": ["general", "infra/deploy"], "name": "demo", "rest_port": 1})


@pytest.fixture()
def config(tmp_path):
    return make_config(tmp_path)
