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

from typing import TYPE_CHECKING, Mapping, TypeVar, get_args

from cwl_utils.parser import Workflow

if TYPE_CHECKING:
    from cwl_utils.parser import Process

T = TypeVar("T")
ORIGINAL_CWLVERSION = "http://commonwl.org/cwltool#original_cwlVersion"


def to_index(collection: list[T]) -> Mapping[str, T]:
    result: dict[str, T] = {}

    for item in collection:
        id = getattr(item, "id", None)
        if id:
            result[id] = item

    return result


def search_process(process_id: str, process: Process | list[Process]) -> Process | None:
    if isinstance(process, list):
        for wf in process:
            if process_id == wf.id:
                return wf
    elif process_id == process.id:
        return process

    return None


def contains_process(process_id: str, process: Process | list[Process]) -> bool:
    return search_process(process_id=process_id, process=process) is not None


def get_ids(process: Process | list[Process]) -> list[str]:
    return [p.id for p in process] if isinstance(process, list) else [process.id]


def assert_process_contained(process_id: str, process: Process | list[Process]):
    if not contains_process(process_id=process_id, process=process):
        raise ValueError(
            f"Process {process_id} does not exist in input CWL document, only {get_ids(process)} available."
        )


def _clean_part(value: str, separator: str | None = "/") -> str:
    return value.split(separator)[-1]


def _clean_values(
    value: str | list[str], separator: str | None = "/"
) -> str | list[str]:
    if isinstance(value, list):
        return [_clean_part(value=e, separator=separator) for e in value]

    return _clean_part(value=value, separator=separator)


def _remove_parameter_refs(process: Process):
    for parameters in (process.inputs, process.outputs):
        for parameter in parameters:
            parameter.id = _clean_part(parameter.id)
            if hasattr(parameter, "outputSource"):
                parameter.outputSource = _clean_values(
                    parameter.outputSource, f"#{process.id}/"
                )


def _remove_step_refs(process: Process):
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


def _remove_process_refs(process: Process):
    process.id = _clean_part(process.id, "#")
    _remove_parameter_refs(process)
    _remove_step_refs(process)

    if process.extension_fields and ORIGINAL_CWLVERSION in process.extension_fields:
        process.extension_fields.pop(ORIGINAL_CWLVERSION)


def remove_refs(process: Process | list[Process]):
    for current in process if isinstance(process, list) else [process]:
        _remove_process_refs(current)


def assert_connected_graph(process: Process | list[Process]):
    index: Mapping[str, Process] = (
        to_index(process) if isinstance(process, list) else {process.id: process}
    )
    issues: list[str] = []

    for process in index.values():
        if any(isinstance(process, typ) for typ in get_args(Workflow)):
            for step in getattr(process, "steps", []):
                if isinstance(step.run, str) and not index.get(step.run[1:]):
                    issues.append(f"- {process.id}.steps.{step.id} = {step.run}")

    if issues:
        nl = "\n"
        raise ValueError(
            f"Detected unresolved links in the input $graph:\n{nl.join(issues)}"
        )
