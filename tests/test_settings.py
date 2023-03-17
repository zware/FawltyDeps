"""Test how settings cascade/combine across command-line, config file, etc."""
import argparse
import logging
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Type, Union

import pytest
from hypothesis import given, strategies
from pydantic import ValidationError
from pydantic.env_settings import SettingsError  # pylint: disable=no-name-in-module

from fawltydeps.main import build_parser
from fawltydeps.settings import Action, OutputFormat, Settings
from fawltydeps.types import TomlData

if sys.version_info >= (3, 11):
    from tomllib import TOMLDecodeError  # pylint: disable=no-member
else:
    from tomli import TOMLDecodeError

EXPECT_DEFAULTS = dict(
    actions={Action.REPORT_UNDECLARED, Action.REPORT_UNUSED},
    code={Path(".")},
    deps={Path(".")},
    pyenv=None,
    output_format=OutputFormat.HUMAN_SUMMARY,
    ignore_undeclared=set(),
    ignore_unused=set(),
    deps_parser_choice=None,
    verbosity=0,
)


def run_build_settings(cmdl: List[str], config_file: Optional[Path] = None) -> Settings:
    """Combine the two relevant function calls to get a Settings."""
    parser = build_parser()
    args = parser.parse_args(cmdl)
    return Settings.config(config_file=config_file).create(args)


def make_settings_dict(**kwargs):
    """Create an expected version of Settings.dict(), with customizations.

    Return a copy of EXPECT_DEFAULTS, with the given customizations applied.
    """
    ret = EXPECT_DEFAULTS.copy()
    ret.update(kwargs)
    return ret


@pytest.fixture
def setup_env(monkeypatch):
    """Allow setup of fawltydeps_* env vars in a test case"""

    def _inner(**kwargs: str):
        for k, v in kwargs.items():
            monkeypatch.setenv(f"fawltydeps_{k}", v)

    return _inner


safe_string = strategies.text(alphabet=string.ascii_letters + string.digits, min_size=1)
nonempty_string_set = strategies.sets(safe_string, min_size=1)
three_different_string_groups = strategies.tuples(
    nonempty_string_set, nonempty_string_set, nonempty_string_set
).filter(lambda ss: ss[0] != ss[1] and ss[0] != ss[2] and ss[1] != ss[2])


@given(code_deps_base=three_different_string_groups)
def test_code_deps_and_base_unequal__raises_error(code_deps_base):
    code, deps, base = code_deps_base
    args = list(base) + ["--code"] + list(code) + ["--deps"] + list(deps)
    with pytest.raises(argparse.ArgumentError):
        run_build_settings(args)


@given(basepaths=nonempty_string_set, fillers=nonempty_string_set)
@pytest.mark.parametrize(["filled", "unfilled"], [("code", "deps"), ("deps", "code")])
def test_base_path_respects_path_already_filled_via_cli(
    basepaths, filled, unfilled, fillers
):
    args = list(basepaths) + [f"--{filled}"] + list(fillers)
    settings = run_build_settings(args)
    assert getattr(settings, filled) == to_path_set(fillers)
    assert getattr(settings, unfilled) == to_path_set(basepaths)


@given(basepaths=nonempty_string_set)
def test_base_path_fills_code_and_deps_when_other_path_settings_are_absent(basepaths):
    # Nothing else through CLI nor through config file
    settings = run_build_settings(cmdl=list(basepaths))
    expected = to_path_set(basepaths)
    assert settings.code == expected
    assert settings.deps == expected


@pytest.mark.parametrize(
    ["config_settings", "basepaths"],
    [
        pytest.param(conf_sett, base, id=test_name)
        for conf_sett, base, test_name in [
            (None, {"single-base"}, "empty-config"),
            (dict(code=["test-code"]), {"base1", "base2"}, "only-code-set"),
            (dict(deps=["deps-test"]), {"single-base"}, "only-deps-set"),
            (
                dict(code=["code-test"], deps=["test-deps"]),
                {"base1", "base2"},
                "code-and-deps-set",
            ),
        ]
    ],
)
def test_base_path_overrides_config_file_code_and_deps(
    config_settings,
    basepaths,
    setup_fawltydeps_config,
):
    config_file = (
        None if config_settings is None else setup_fawltydeps_config(config_settings)
    )

    settings = run_build_settings(cmdl=list(basepaths), config_file=config_file)
    expected = to_path_set(basepaths)
    assert settings.code == expected
    assert settings.deps == expected


@dataclass
class SettingsTestVector:
    """Test vectors for FawltyDeps Settings configuration."""

    id: str
    config_settings: Optional[Union[str, TomlData]] = None
    env_settings: Dict[str, Any] = field(default_factory=dict)
    cmdline_settings: Dict[str, Any] = field(default_factory=dict)
    expect: Union[Dict[str, Any], Type[Exception]] = field(
        default_factory=lambda: EXPECT_DEFAULTS
    )


settings_test_vector = [
    SettingsTestVector("no_config_file__uses_defaults"),
    SettingsTestVector("empty_config_file__uses_defaults", config_settings=""),
    SettingsTestVector("empty_config_file_section__uses_defaults", config_settings={}),
    SettingsTestVector(
        "config_file_invalid_toml__raises_TOMLDecodeError",
        config_settings="THIS IS BOGUS TOML",
        expect=TOMLDecodeError,
    ),
    SettingsTestVector(
        "config_file_unsupported_fields__raises_ValidationError",
        config_settings=dict(
            code="my_code_dir", not_supported=123
        ),  # unsupported directive
        expect=ValidationError,
    ),
    SettingsTestVector(
        "config_file_invalid_values__raises_ValidationError",
        config_settings=dict(actions="list_imports"),  # actions is not a list
        expect=ValidationError,
    ),
    SettingsTestVector(
        "config_file__overrides_some_defaults",
        config_settings=dict(actions=["list_deps"], deps=["my_requirements.txt"]),
        expect=make_settings_dict(
            actions={Action.LIST_DEPS}, deps={Path("my_requirements.txt")}
        ),
    ),
    SettingsTestVector(
        "env_var_with_wrong_type__raises_SettingsError",
        env_settings=dict(actions="list_imports"),  # actions is not a list
        expect=SettingsError,
    ),
    SettingsTestVector(
        "env_var_with_invalid_value__raises_SettingsError",
        env_settings=dict(
            ignore_unused='["foo", "missing_quote]'
        ),  # cannot parse value
        expect=SettingsError,
    ),
    SettingsTestVector(
        "env_vars__overrides_some_defaults",
        env_settings=dict(actions='["list_imports"]', ignore_unused='["foo", "bar"]'),
        expect=make_settings_dict(
            actions={Action.LIST_IMPORTS}, ignore_unused={"foo", "bar"}
        ),
    ),
    SettingsTestVector(
        "config_file_and_env_vars__overrides_separate_defaults",
        config_settings=dict(code=["my_code_dir"], deps=["my_requirements.txt"]),
        env_settings=dict(actions='["list_imports"]', ignore_unused='["foo", "bar"]'),
        expect=make_settings_dict(
            actions={Action.LIST_IMPORTS},
            code={Path("my_code_dir")},
            deps={Path("my_requirements.txt")},
            ignore_unused={"foo", "bar"},
        ),
    ),
    SettingsTestVector(
        "config_file_and_env_vars__env_overrides_file",
        config_settings=dict(code="my_code_dir", deps=["my_requirements.txt"]),
        env_settings=dict(actions='["list_imports"]', code='["<stdin>"]'),
        expect=make_settings_dict(
            actions={Action.LIST_IMPORTS},
            code={"<stdin>"},
            deps={Path("my_requirements.txt")},
        ),
    ),
    SettingsTestVector(
        "cmd_line_unsupported_field__is_ignored",
        cmdline_settings=dict(unsupported=123),  # unsupported Settings field
    ),
    SettingsTestVector(
        "cmd_line_invalid_value__raises_ValidationError",
        cmdline_settings=dict(actions="['wrong_action']"),  # invalid enum value
        expect=ValidationError,
    ),
    SettingsTestVector(
        "cmd_line_wrong_type__raises_ValidationError",
        cmdline_settings=dict(actions="list_imports"),  # should be list/set, not str
        expect=ValidationError,
    ),
    SettingsTestVector(
        "cmd_line__overrides_some_defaults",
        cmdline_settings=dict(
            actions={Action.LIST_IMPORTS}, ignore_unused={"foo", "bar"}
        ),
        expect=make_settings_dict(
            actions={Action.LIST_IMPORTS}, ignore_unused={"foo", "bar"}
        ),
    ),
    SettingsTestVector(
        "cmd_line__overrides_config_file",
        config_settings=dict(code="my_code_dir", deps=["my_requirements.txt"]),
        cmdline_settings=dict(actions={Action.LIST_IMPORTS}, code={"<stdin>"}),
        expect=make_settings_dict(
            actions={Action.LIST_IMPORTS},
            code={"<stdin>"},
            deps={Path("my_requirements.txt")},
        ),
    ),
    SettingsTestVector(
        "cmd_line__verbose_minus_quiet__determines_verbosity",
        cmdline_settings=dict(verbose=3, quiet=5),
        expect=make_settings_dict(verbosity=-2),
    ),
    SettingsTestVector(
        "cmd_line__verbose__overrides_env_verbosity",
        env_settings=dict(verbosity="1"),
        cmdline_settings=dict(verbose=2),
        expect=make_settings_dict(verbosity=2),
    ),
    SettingsTestVector(
        "cmd_line__no_verbose_no_quiet__uses_underlying_verbosity",
        config_settings=dict(verbosity=-1),
        expect=make_settings_dict(verbosity=-1),
    ),
    SettingsTestVector(
        "cmd_line_env_var_and_config_file__cascades",
        config_settings=dict(
            actions='["list_imports"]',
            code=["my_code_dir"],
            deps=["my_requirements.txt"],
            verbosity=1,
        ),
        env_settings=dict(actions='["list_deps"]', code='["<stdin>"]'),
        cmdline_settings=dict(code=["my_notebook.ipynb"], verbose=2, quiet=4),
        expect=make_settings_dict(
            actions={Action.LIST_DEPS},  # env overrides config file
            code={Path("my_notebook.ipynb")},  # cmd line overrides env + config file
            deps={Path("my_requirements.txt")},  # from config file
            verbosity=-2,  # calculated from cmd line, overrides config file
        ),
    ),
]


@pytest.mark.parametrize(
    "vector", [pytest.param(v, id=v.id) for v in settings_test_vector]
)
def test_settings(
    vector,
    setup_fawltydeps_config,
    setup_env,
):  # pylint: disable=too-many-arguments
    if vector.config_settings is None:
        config_file = None
    else:
        config_file = setup_fawltydeps_config(vector.config_settings)
    setup_env(**vector.env_settings)
    cmdline_args = argparse.Namespace(**vector.cmdline_settings)
    if isinstance(vector.expect, dict):
        settings = Settings.config(config_file=config_file).create(cmdline_args)
        assert settings.dict() == vector.expect
    else:  # Assume we expect an exception
        with pytest.raises(vector.expect):
            Settings.config(config_file=config_file).create(cmdline_args)


def test_settings__instance__is_immutable():
    settings = Settings.config(config_file=None)()
    with pytest.raises(TypeError):
        settings.code = ["<stdin>"]
    assert settings.dict() == make_settings_dict()


def test_settings__missing_config_file__uses_defaults_and_warns(tmp_path, caplog):
    missing_file = tmp_path / "MISSING.toml"
    caplog.set_level(logging.INFO)
    settings = Settings.config(config_file=missing_file)()
    assert settings.dict() == make_settings_dict()
    assert "Failed to load configuration file:" in caplog.text
    assert str(missing_file) in caplog.text


def to_path_set(ps: Iterable[str]) -> Set[Path]:
    return set(map(Path, ps))
