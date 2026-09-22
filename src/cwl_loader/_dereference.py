# Copyright 2025 Terradue
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urldefrag

from cwl_utils.parser import Workflow
from loguru import logger

from .utils import contains_process, get_ids, to_index

ORIGINAL_CWLVERSION = "http://commonwl.org/cwltool#original_cwlVersion"

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any, Protocol

    import requests
    from cwl_utils.parser import Process

    class CwlLoader(Protocol):
        def __call__(
            self,
            *,
            path: str,
            session: requests.Session,
        ) -> Process | list[Process]: ...


def _as_process_list(process: Process | list[Process]) -> list[Process]:
    return process if isinstance(process, list) else [process]


def _clean_part(value: str, separator: str | None = "/") -> str:
    return value.split(separator)[-1]


def _clean_values(
    value: str | list[str], separator: str | None = "/"
) -> str | list[str]:
    if isinstance(value, list):
        return [_clean_part(value=item, separator=separator) for item in value]

    return _clean_part(value=value, separator=separator)


def _remove_parameter_refs(process: Process) -> None:
    for parameters in (process.inputs, process.outputs):
        for parameter in parameters:
            parameter.id = _clean_part(parameter.id)
            if hasattr(parameter, "outputSource") and parameter.outputSource:
                parameter.outputSource = _clean_values(
                    parameter.outputSource, f"#{process.id}/"
                )


def _remove_step_refs(process: Process) -> None:
    for step in getattr(process, "steps", []):
        step.id = _clean_part(step.id)

        for step_in in getattr(step, "in_", []):
            step_in.id = _clean_part(step_in.id)
            if step_in.source:
                step_in.source = _clean_values(step_in.source, f"#{process.id}/")

        if getattr(step, "out", None):
            step.out = _clean_values(step.out)
        if getattr(step, "run", None):
            step.run = step.run[step.run.rfind("#") :]
        if getattr(step, "scatter", None):
            step.scatter = _clean_values(step.scatter, f"#{process.id}/")


def _remove_process_refs(process: Process) -> None:
    process.id = _clean_part(process.id, "#")
    _remove_parameter_refs(process)
    _remove_step_refs(process)

    if process.extension_fields and ORIGINAL_CWLVERSION in process.extension_fields:
        process.extension_fields.pop(ORIGINAL_CWLVERSION)


def remove_refs(process: Process | list[Process]) -> None:
    for current in _as_process_list(process):
        _remove_process_refs(current)


def _select_referenced_process(
    referenced: Process | list[Process],
    referenced_index: Mapping[str, Process],
    fragment: str,
    step: Any,
    parent: Process,
) -> Process | list[Process]:
    if not fragment:
        return referenced
    if fragment not in referenced_index:
        raise Exception(
            f"Step {step.id} in {parent.id} declares an illegal run {step.run} "
            f"where {fragment} ID does not exist, only {get_ids(referenced)} available."
        )
    return referenced_index[fragment]


def _append_referenced_process(
    current: Process,
    process: Process | list[Process],
    accumulator: list[Process],
    step: Any,
    run_url: str,
) -> None:
    if contains_process(current.id, process):
        raise Exception(
            f"Cannot import {current.class_} {current.id} declared in {run_url}, "
            "'id' already present in embedding CWL document"
        )
    accumulator.append(current)
    step.run = f"#{current.id}"


def _dereference_step(
    step: Any,
    parent: Process,
    process: Process | list[Process],
    accumulator: list[Process],
    uri: str,
    session: requests.Session,
    loader: CwlLoader,
) -> None:
    logger.debug(f"Checking if {step.run} must be externally imported...")
    run_url, fragment = urldefrag(step.run)
    logger.debug(f"run_url: {run_url} - uri: {uri}")

    if not run_url or uri == run_url:
        return

    referenced = loader(path=run_url, session=session)
    referenced_index = to_index(referenced) if isinstance(referenced, list) else {}
    referenced = _select_referenced_process(
        referenced, referenced_index, fragment, step, parent
    )

    if isinstance(referenced, list):
        if len(referenced) != 1:
            raise ValueError(
                f"No entry point provided for $graph referenced by {step.run}"
            )
        _append_referenced_process(referenced[0], process, accumulator, step, run_url)
        return

    _append_referenced_process(referenced, process, accumulator, step, run_url)
    if isinstance(referenced, Workflow):
        for inner_step in getattr(referenced, "steps", []):
            accumulator.append(referenced_index[inner_step.run.split("#")[-1]])


def _rebase_inline_references(process: Process, old: str, new: str) -> None:
    """Rebase anonymous process IDs and their local wiring before cleanup."""

    def rebase(value):
        if isinstance(value, list):
            return [rebase(item) for item in value]
        if isinstance(value, str) and (value == old or value.startswith(f"{old}/")):
            return new + value[len(old) :]
        return value

    process.id = new
    for parameter in [*process.inputs, *process.outputs]:
        parameter.id = rebase(parameter.id)
        if hasattr(parameter, "outputSource"):
            parameter.outputSource = rebase(parameter.outputSource)
    for step in getattr(process, "steps", []):
        step.id = rebase(step.id)
        for item in step.in_:
            item.id = rebase(item.id)
            item.source = rebase(item.source)
        step.out = rebase(step.out)
        step.scatter = rebase(step.scatter)
        if isinstance(step.run, str):
            step.run = rebase(step.run)


def _lift_inline_processes(processes: list[Process], uri: str) -> None:
    """Index inline runs (including nested workflows) before dereferencing URLs."""
    index = {p.id: p for p in processes}
    # Appending while iterating also visits newly lifted inline workflows.
    for parent in processes:
        for step in getattr(parent, "steps", []):
            if isinstance(step.run, str):
                continue
            embedded = step.run
            if embedded.id.startswith("_:"):
                fragment = step.id.split("#")[-1]
                identifier = f"{urldefrag(uri)[0]}#{fragment}/run"
                _rebase_inline_references(embedded, embedded.id, identifier)
            existing = index.get(embedded.id)
            if existing is not None and existing is not embedded:
                raise ValueError(f"Duplicate inline process identifier: {embedded.id}")
            if existing is None:
                index[embedded.id] = embedded
                processes.append(embedded)
            # An indexed inline process must not be fetched as an external URL.
            step.run = f"#{embedded.id.split('#')[-1]}"


def _dereference_steps(
    process: Process | list[Process],
    uri: str,
    session: requests.Session,
    loader: CwlLoader,
) -> list[Process]:
    result = list(_as_process_list(process))
    _lift_inline_processes(result, uri)
    for parent in result:
        for step in getattr(parent, "steps", []):
            _dereference_step(
                step,
                parent,
                result,
                result,
                uri,
                session,
                loader,
            )

    return result
