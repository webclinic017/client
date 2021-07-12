# -*- coding: utf-8 -*-
"""Sweep controller.

This module implements the sweep controller.

On error an exception is raised:
    ControllerError

Example:
    import wandb

    #
    # create a sweep controller
    #

    # There are two different ways sweeps can be created:
    # (1) create with sweep id from `wandb sweep` command
    sweep_id = 'xyzxyz2'
    tuner = wandb.controller(sweep_id)
    # (2) create with sweep config
    sweep_config = {}
    tuner = wandb.controller()
    tuner.configure(sweep_config)
    tuner.create()

    #
    # run the sweep controller
    #

    # There are three different ways sweeps can be executed:
    # (1) run to completion
    tuner.run()
    # (2) run in a simple loop
    while not tuner.done():
        tuner.step()
        tuner.print_status()
    # (3) run in a more complex loop
    while not tuner.done():
        params = tuner.search()
        tuner.schedule(params)
        runs = tuner.stopping()
        if runs:
            tuner.stop_runs(runs)
"""

from __future__ import print_function

import copy
import json
import os
import random
import string
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from wandb import env
from wandb.apis import InternalApi
from wandb.sdk import wandb_sweep
from wandb.util import get_module, handle_sweep_config_violations
import yaml

# TODO(jhr): Add metric status
# TODO(jhr): Add print_space
# TODO(jhr): Add print_summary

sweeps = get_module(
    "wandb.sweeps",
    required="This module requires wandb to be built with the local "
    "controller. Please run pip install wandb[sweeps].",
)

# This should be something like 'pending' (but we need to make sure everyone else is ok with that)
SWEEP_INITIAL_RUN_STATE = sweeps.RunState.proposed


def _id_generator(size=10, chars=string.ascii_lowercase + string.digits):
    return "".join(random.choice(chars) for _ in range(size))


class ControllerError(Exception):
    """Base class for sweep errors"""

    pass


class _WandbController:
    """Sweep controller class.

    Internal datastructures on the sweep object to coordinate local controller with
    cloud controller.

    Data structures:
        controller: {
            schedule: [
                { id: SCHEDULE_ID
                  data: {param1: val1, param2: val2}},
            ]
            earlystop: [RUN_ID, ...]
        scheduler:
            scheduled: [
                { id: SCHEDULE_ID
                  runid: RUN_ID},
            ]

    `controller` is only updated by the client
    `scheduler` is only updated by the cloud backend

    Protocols:
        Scheduling a run:
        - client controller adds a schedule entry on the controller.schedule list
        - cloud backend notices the new entry and creates a run with the parameters
        - cloud backend adds a scheduled entry on the scheduler.scheduled list
        - client controller notices that the run has been scheduled and removes it from
          controller.schedule list

    Current implementation details:
        - Runs are only schedule if there are no other runs scheduled.

    """

    def __init__(self, sweep_id_or_config=None, entity=None, project=None):

        # sweep id configured in constuctor
        self._sweep_id: Optional[str] = None

        # configured parameters
        # Configuration to be created
        self._create: Dict = {}
        # Custom search
        self._custom_search: Optional[
            Callable[
                [Union[dict, sweeps.SweepConfig], List[sweeps.SweepRun]],
                Optional[sweeps.SweepRun],
            ]
        ] = None
        # Custom stopping
        self._custom_stopping: Optional[
            Callable[
                [Union[dict, sweeps.SweepConfig], List[sweeps.SweepRun]],
                List[sweeps.SweepRun],
            ]
        ] = None
        # Program function (used for future jupyter support)
        self._program_function = None

        # The following are updated every sweep step
        # raw sweep object (dict of strings)
        self._sweep_obj = None
        # parsed sweep config (dict)
        self._sweep_config: Optional[Union[dict, sweeps.SweepConfig]] = None
        # sweep metric used to optimize (str or None)
        self._sweep_metric: Optional[str] = None
        # list of _Run objects
        self._sweep_runs: Optional[List[sweeps.SweepRun]] = None
        # dictionary mapping name of run to run object
        self._sweep_runs_map: Optional[Dict[str, sweeps.SweepRun]] = None
        # scheduler dict (read only from controller) - used as feedback from the server
        self._scheduler: Optional[Dict] = None
        # controller dict (write only from controller) - used to send commands to server
        self._controller: Optional[Dict] = None
        # keep track of controller dict from previous step
        self._controller_prev_step: Optional[Dict] = None

        # Internal
        # Keep track of whether the sweep has been started
        self._started: bool = False
        # indicate whether there is more to schedule
        self._done_scheduling: bool = False
        # indicate whether the sweep needs to be created
        self._defer_sweep_creation: bool = False
        # count of logged lines since last status
        self._logged: int = 0
        # last status line printed
        self._laststatus: str = ""
        # keep track of logged actions for print_actions()
        self._log_actions: List[Tuple[str, str]] = []
        # keep track of logged debug for print_debug()
        self._log_debug: List[str] = []

        # all backend commands use internal api
        environ = os.environ
        if entity:
            env.set_entity(entity, env=environ)
        if project:
            env.set_project(project, env=environ)
        self._api = InternalApi(environ=environ)

        if isinstance(sweep_id_or_config, str):
            self._sweep_id = sweep_id_or_config
        elif isinstance(sweep_id_or_config, dict) or isinstance(
            sweep_id_or_config, sweeps.SweepConfig
        ):
            self._create = sweeps.SweepConfig(sweep_id_or_config)

            # check for custom search and or stopping functions
            for config_key, controller_attr in zip(
                ["method", "early_terminate"], ["_custom_search", "_custom_stopping"]
            ):
                if callable(config_key in self._create and self._create[config_key]):
                    setattr(self, controller_attr, self._create[config_key])
                    self._create[config_key] = "custom"

            self._sweep_id = self.create(from_dict=True)
        elif sweep_id_or_config is None:
            self._defer_sweep_creation = True
            return
        else:
            raise ControllerError("Unhandled sweep controller type")
        sweep_obj = self._sweep_object_read_from_backend()
        if sweep_obj is None:
            raise ControllerError("Can not find sweep")
        self._sweep_obj = sweep_obj

    def configure(self, key: str, value: Any) -> None:
        self._configure_check()
        proposed = copy.deepcopy(self._create)
        proposed[key] = value

        # this will throw an exception if the config is invalid
        new_config = sweeps.SweepConfig(proposed)

        # if no exception is thrown then we overwrite
        self._create = new_config

    @property
    def sweep_config(self) -> Union[dict, sweeps.SweepConfig]:
        return self._sweep_config

    @property
    def sweep_id(self) -> str:
        return self._sweep_id

    def _log(self) -> None:
        self._logged += 1

    def _error(self, s: str) -> None:
        print("ERROR:", s)
        self._log()

    def _warn(self, s: str) -> None:
        print("WARN:", s)
        self._log()

    def _info(self, s: str) -> None:
        print("INFO:", s)
        self._log()

    def _debug(self, s: str) -> None:
        print("DEBUG:", s)
        self._log()

    def _configure_check(self) -> None:
        if self._started:
            raise ControllerError("Can not configure after sweep has been started.")

    def create(self, from_dict: bool = False) -> str:
        if self._started:
            raise ControllerError("Can not create after sweep has been started.")
        if not self._defer_sweep_creation and not from_dict:
            raise ControllerError("Can not use create on already created sweep.")
        if not self._create:
            raise ControllerError("Must configure sweep before create.")

        # Create sweep
        sweep_id, warnings = self._api.upsert_sweep(self._create)
        handle_sweep_config_violations(warnings)

        print("Create sweep with ID:", sweep_id)
        sweep_url = wandb_sweep._get_sweep_url(self._api, sweep_id)
        if sweep_url:
            print("Sweep URL:", sweep_url)
        self._sweep_id = sweep_id
        self._defer_sweep_creation = False
        return sweep_id

    def run(
        self,
        verbose: bool = False,
        print_status: bool = True,
        print_actions: bool = False,
        print_debug: bool = False,
    ) -> None:
        if verbose:
            print_status = True
            print_actions = True
            print_debug = True
        self._start_if_not_started()
        while not self.done():
            if print_status:
                self.print_status()
            self.step()
            if print_actions:
                self.print_actions()
            if print_debug:
                self.print_debug()
            time.sleep(5)

    def _sweep_object_read_from_backend(self) -> Optional[dict]:
        specs_json = {}
        if self._sweep_metric:
            k = ["_step"]
            k.append(self._sweep_metric)
            specs_json = {"keys": k, "samples": 100000}
        specs = json.dumps(specs_json)
        # TODO(jhr): catch exceptions?
        sweep_obj = self._api.sweep(self._sweep_id, specs)
        if not sweep_obj:
            return
        self._sweep_obj = sweep_obj
        self._sweep_config = yaml.safe_load(sweep_obj["config"])
        self._sweep_metric = self._sweep_config.get("metric", {}).get("name")

        _sweep_runs: List[sweeps.SweepRun] = []
        for r in sweep_obj["runs"]:
            rr = r.copy()
            if rr["summaryMetrics"]:
                rr["summaryMetrics"] = json.loads(rr["summaryMetrics"])
            rr["config"] = json.loads(rr["config"])
            if rr["history"]:
                rr["history"] = [json.loads(d) for d in rr["history"]]
            _sweep_runs.append(sweeps.SweepRun(**rr))

        self._sweep_runs = _sweep_runs
        self._sweep_runs_map = {r.name: r for r in self._sweep_runs}

        self._controller = json.loads(sweep_obj.get("controller") or "{}")
        self._scheduler = json.loads(sweep_obj.get("scheduler") or "{}")
        self._controller_prev_step = self._controller.copy()
        return sweep_obj

    def _sweep_object_sync_to_backend(self) -> None:
        if self._controller == self._controller_prev_step:
            return
        sweep_obj_id = self._sweep_obj["id"]
        controller = json.dumps(self._controller)
        _, warnings = self._api.upsert_sweep(
            self._sweep_config, controller=controller, obj_id=sweep_obj_id
        )
        handle_sweep_config_violations(warnings)
        self._controller_prev_step = self._controller.copy()

    def _start_if_not_started(self) -> None:
        if self._started:
            return
        if self._defer_sweep_creation:
            raise ControllerError(
                "Must specify or create a sweep before running controller."
            )
        obj = self._sweep_object_read_from_backend()
        if not obj:
            return
        is_local = self._sweep_config.get("controller", {}).get("type") == "local"
        if not is_local:
            raise ControllerError(
                "Only sweeps with a local controller are currently supported."
            )
        self._started = True
        # reset controller state, we might want to parse this and decide
        # what we can continue and add a version key, but for now we can
        # be safe and just reset things on start
        self._controller = {}
        self._sweep_object_sync_to_backend()

    def _parse_scheduled(self):
        scheduled_list = self._scheduler.get("scheduled") or []
        started_ids = []
        stopped_runs = []
        done_runs = []
        for s in scheduled_list:
            runid = s.get("runid")
            objid = s.get("id")
            r = self._sweep_runs_map.get(runid)
            if not r:
                continue
            if r.stopped:
                stopped_runs.append(runid)
            summary = r.summary_metrics
            if r.state == SWEEP_INITIAL_RUN_STATE and not summary:
                continue
            started_ids.append(objid)
            if r.state != "running":
                done_runs.append(runid)
        return started_ids, stopped_runs, done_runs

    def _step(self) -> None:
        self._start_if_not_started()
        self._sweep_object_read_from_backend()

        started_ids, stopped_runs, done_runs = self._parse_scheduled()

        # Remove schedule entry from controller dict if already scheduled
        schedule_list = self._controller.get("schedule", [])
        new_schedule_list = [s for s in schedule_list if s.get("id") not in started_ids]
        self._controller["schedule"] = new_schedule_list

        # Remove earlystop entry from controller if already stopped
        earlystop_list = self._controller.get("earlystop", [])
        new_earlystop_list = [
            r for r in earlystop_list if r not in stopped_runs and r not in done_runs
        ]
        self._controller["earlystop"] = new_earlystop_list

        # Clear out step logs
        self._log_actions = []
        self._log_debug = []

    def step(self) -> None:
        self._step()
        suggestion = self.search()
        self.schedule(suggestion)
        to_stop = self.stopping()
        if len(to_stop) > 0:
            self.stop_runs(to_stop)

    def done(self) -> bool:
        self._start_if_not_started()
        state = self._sweep_obj.get("state")
        if state in ("RUNNING", "PENDING"):
            return False
        return True

    def _search(self) -> Optional[sweeps.SweepRun]:
        search = self._custom_search or sweeps.next_run
        next_run = search(self._sweep_config, self._sweep_runs or [])
        if next_run is None:
            self._done_scheduling = True
        return next_run

    def search(self) -> Optional[sweeps.SweepRun]:
        self._start_if_not_started()
        suggestion = self._search()
        return suggestion

    def _stopping(self) -> List[sweeps.SweepRun]:
        stopper = self._custom_stopping or sweeps.stop_runs
        stop_runs = stopper(self._sweep_config, self._sweep_runs or [])

        debug_lines = "\n".join(
            [
                " ".join([f"{k}={v}" for k, v in run.early_terminate_info.items()])
                for run in stop_runs
                if run.early_terminate_info is not None
            ]
        )
        if debug_lines:
            self._log_debug += debug_lines

        return stop_runs

    def stopping(self) -> List[sweeps.SweepRun]:
        self._start_if_not_started()
        return self._stopping()

    def schedule(self, run: sweeps.SweepRun) -> None:
        self._start_if_not_started()

        # only schedule one run at a time (for now)
        if self._controller and self._controller.get("schedule"):
            return

        param_list = ["%s=%s" % (k, v.get("value")) for k, v in sorted(run.config)]
        self._log_actions.append(("schedule", ",".join(param_list)))

        # schedule one run
        schedule_list = []
        schedule_id = _id_generator()
        schedule_list.append({"id": schedule_id, "data": {"args": run.config}})
        self._controller["schedule"] = schedule_list
        self._sweep_object_sync_to_backend()

    def stop_runs(self, runs: List[sweeps.SweepRun]) -> None:
        earlystop_list = list(set(runs))
        self._log_actions.append(("stop", ",".join([r.__repr__() for r in runs])))
        self._controller["earlystop"] = earlystop_list
        self._sweep_object_sync_to_backend()

    def print_status(self) -> None:
        status = _sweep_status(self._sweep_obj, self._sweep_config, self._sweep_runs)
        if self._laststatus != status or self._logged:
            print(status)
        self._laststatus = status
        self._logged = 0

    def print_actions(self) -> None:
        for action, line in self._log_actions:
            self._info("%s (%s)" % (action.capitalize(), line))
        self._log_actions = []

    def print_debug(self) -> None:
        for line in self._log_debug:
            self._debug(line)
        self._log_debug = []

    def print_space(self) -> None:
        self._warn("Method not implemented yet.")

    def print_summary(self) -> None:
        self._warn("Method not implemented yet.")


def controller(
    sweep_id_or_config: Union[str, Dict], entity: str = None, project: str = None
) -> _WandbController:
    """Public sweep controller constructor.

    Usage:
        import wandb
        tuner = wandb.controller(...)
        print(tuner.sweep_config)
        print(tuner.sweep_id)
        tuner.configure_search(...)
        tuner.configure_stopping(...)

    """
    c = _WandbController(
        sweep_id_or_config=sweep_id_or_config, entity=entity, project=project
    )
    return c


def _get_run_counts(runs):
    metrics = {}
    categories = ("running", "finished", "crashed", "failed")
    for r in runs:
        state = r.state
        found = "unknown"
        for c in categories:
            if state == c:
                found = c
                break
        metrics.setdefault(found, 0)
        metrics[found] += 1
    return metrics


def _get_runs_status(metrics):
    categories = ("finished", "crashed", "failed", "unknown", "running")
    mlist = []
    for c in categories:
        if not metrics.get(c):
            continue
        mlist.append("%s: %d" % (c.capitalize(), metrics[c]))
    s = ", ".join(mlist)
    return s


def _sweep_status(
    sweep_obj: dict,
    sweep_conf: Union[dict, sweeps.SweepConfig],
    sweep_runs: List[sweeps.SweepRun],
) -> str:
    sweep = sweep_obj["name"]
    _ = sweep_obj["state"]
    run_count = len(sweep_runs)
    run_type_counts = _get_run_counts(sweep_runs)
    stopped = len([r for r in sweep_runs if r.stopped])
    stopping = len([r for r in sweep_runs if r.should_stop])
    stopstr = ""
    if stopped or stopping:
        stopstr = "Stopped: %d" % stopped
        if stopping:
            stopstr += " (Stopping: %d)" % stopping
    runs_status = _get_runs_status(run_type_counts)
    method = sweep_conf.get("method", "unknown")
    stopping = sweep_conf.get("early_terminate", None)
    sweep_options = []
    sweep_options.append(method)
    if stopping:
        sweep_options.append(stopping.get("type", "unknown"))
    sweep_options = ",".join(sweep_options)
    sections = []
    sections.append("Sweep: %s (%s)" % (sweep, sweep_options))
    if runs_status:
        sections.append("Runs: %d (%s)" % (run_count, runs_status))
    else:
        sections.append("Runs: %d" % (run_count))
    if stopstr:
        sections.append(stopstr)
    sections = " | ".join(sections)
    return sections
