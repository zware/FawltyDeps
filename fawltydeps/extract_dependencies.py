"Collect declared dependencies of the project"

import ast
import logging
import os
import sys
from pathlib import Path
from typing import Any, Iterator, Tuple

import tomli
from pkg_resources import parse_requirements

logger = logging.getLogger(__name__)


class DependencyParsingError(Exception):
    "Error raised when parsing of dependency fails"

    def __init__(self, value: ast.AST):
        super().__init__(value)
        self.value = value


def parse_requirements_contents(
    text: str, path_hint: Path
) -> Iterator[Tuple[str, Path]]:
    """
    Extract dependencies (packages names) from the requirement.txt file
    and other following Requirements File Format. For more information, see
    https://pip.pypa.io/en/stable/reference/requirements-file-format/.
    """
    for requirement in parse_requirements(text):
        yield (requirement.key, path_hint)


def parse_setup_contents(text: str, path_hint: Path) -> Iterator[Tuple[str, Path]]:
    """
    Extract dependencies (package names) from setup.py.
    Function call `setup` where dependencies are listed
    is at the outermost level of setup.py file.
    """

    def _extract_deps_from_bottom_level_list(
        deps: ast.AST,
    ) -> Iterator[Tuple[str, Path]]:
        if isinstance(deps, ast.List):
            for element in deps.elts:
                # Python v3.8 changed from ast.Str to ast.Constant
                if isinstance(element, (ast.Constant, ast.Str)):
                    yield from parse_requirements_contents(
                        ast.literal_eval(element), path_hint=path_hint
                    )
        else:
            raise DependencyParsingError(deps)

    def _extract_deps_from_setup_call(node: ast.Call) -> Iterator[Tuple[str, Path]]:
        for keyword in node.keywords:
            try:
                if keyword.arg == "install_requires":
                    yield from _extract_deps_from_bottom_level_list(keyword.value)
                elif keyword.arg == "extras_require":
                    if isinstance(keyword.value, ast.Dict):
                        logger.debug(ast.dump(keyword.value))
                        for elements in keyword.value.values:
                            logger.debug(ast.dump(elements))
                            yield from _extract_deps_from_bottom_level_list(elements)
                    else:
                        raise DependencyParsingError(keyword.value)
            except DependencyParsingError as e:
                logger.debug(e)
                if sys.version_info >= (3, 9):
                    unparsed_content = ast.unparse(e.value)  # pylint: disable=E1101
                else:
                    unparsed_content = ast.dump(e.value)
                logger.warning(
                    "Could not parse contents of `%s`: %s",
                    keyword.arg,
                    unparsed_content,
                )

    def _is_setup_function_call(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id == "setup"
        )

    setup_contents = ast.parse(text, filename=str(path_hint))
    for node in ast.walk(setup_contents):
        if _is_setup_function_call(node):
            # Below line is not checked by mypy, but `_is_setup_function_call`
            # makes sure that `node` is of a proper type.
            yield from _extract_deps_from_setup_call(node.value)  # type: ignore
            break


def parse_poetry_pyproject_dependencies(
    poetry_config: dict, path_hint: Path
) -> Iterator[Tuple[str, Path]]:
    """
    Extract dependencies (package names) from Poetry fields in pyproject.toml
    """

    # Main dependencies
    if "dependencies" in poetry_config:
        for requirement in poetry_config["dependencies"]:
            if requirement != "python":
                yield (requirement, path_hint)
    else:
        logger.debug("Failed to find Poetry dependencies in %s", path_hint)

    # Grouped dependencies
    if "group" in poetry_config:
        for group in poetry_config["group"].values():
            for requirement in group["dependencies"]:
                if requirement != "python":
                    yield (requirement, path_hint)
    else:
        logger.debug("No Poetry grouped dependencies found in %s", path_hint)

    # Extra dependencies
    if "extras" in poetry_config:
        for group in poetry_config["extras"].values():
            for requirement in group:
                yield from parse_requirements_contents(requirement, path_hint)
    else:
        logger.debug("No Poetry extra dependencies found in %s", path_hint)


def parse_pep621_pyproject_main_dependencies(
    parsed_contents: dict, path_hint: Path
) -> Iterator[Tuple[str, Path]]:
    """
    Parse dependencies in pyproject.toml's project.dependencies
    """
    main_dependencies = parsed_contents.get("project", {}).get("dependencies", {})

    for requirement_text in main_dependencies:
        yield from parse_requirements_contents(requirement_text, path_hint)


def parse_pep621_pyproject_optional_dependencies(
    parsed_contents: dict[str, Any], path_hint: Path
) -> Iterator[Tuple[str, Path]]:
    """
    Parse dependencies in pyproject.toml's project.optional-dependencies
    """
    optional_dependencies = (
        parsed_contents.get("project", {}).get("optional-dependencies", {}).values()
    )
    for dependency_group in optional_dependencies:
        for requirement_text in dependency_group:
            yield from parse_requirements_contents(requirement_text, path_hint)


def parse_pep621_pyproject_contents(
    parsed_contents: dict, path_hint: Path
) -> Iterator[Tuple[str, Path]]:
    """
    Extract dependencies (package names) in PEP 621 styled pyproject.toml
    """
    yield from parse_pep621_pyproject_main_dependencies(parsed_contents, path_hint)
    yield from parse_pep621_pyproject_optional_dependencies(parsed_contents, path_hint)


def parse_pyproject_contents(text: str, path_hint: Path) -> Iterator[Tuple[str, Path]]:
    """
    Parse dependencies from specific metadata fields in a pyproject.toml file.
    This can currenly parse dependencies from dependency fields in:
    - PEP 621 core metadata fields
    - Poetry-specific metadata
    """
    parsed_contents = tomli.loads(text)

    if "poetry" in parsed_contents.get("tool", {}):
        yield from parse_poetry_pyproject_dependencies(
            parsed_contents["tool"]["poetry"], path_hint
        )
    elif "project" in parsed_contents:
        yield from parse_pep621_pyproject_contents(parsed_contents, path_hint)
    else:
        logger.error(
            "pyproject.toml does not have the expected format. "
            "Expected [project] or [tool.poetry]."
        )


def extract_dependencies(path: Path) -> Iterator[Tuple[str, Path]]:
    """
    Extract dependencies from supported file types.
    Traverse directory tree to find matching files.

    Generate (i.e. yield) dependency names that are declared in the supported files.
    There is no guaranteed ordering on the dependency names.
    """
    parsers = {
        "requirements.txt": parse_requirements_contents,
        "requirements.in": parse_requirements_contents,
        "setup.py": parse_setup_contents,
        "pyproject.toml": parse_pyproject_contents,
    }

    logger.debug(path)

    if path.is_file():
        logger.debug(path)
        parser = parsers.get(path.name)
        if parser:
            yield from parser(path.read_text(), path_hint=path)
        else:
            logger.error("Parsing file %s is not supported", path.name)

    else:
        for root, _dirs, files in os.walk(path):
            for filename in files:
                if filename in parsers:
                    parser = parsers[filename]
                    current_path = Path(root, filename)
                    logger.debug(f"Extracting dependency from {current_path}.")
                    yield from parser(current_path.read_text(), path_hint=current_path)
