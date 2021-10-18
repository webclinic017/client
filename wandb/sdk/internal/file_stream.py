import base64
import binascii
import collections
import itertools
import logging
import os
import sys
import random
import requests
import threading
import time
from dataclasses import dataclass, field
from typing import List

import wandb
from wandb import util
from wandb import env

import six
from six.moves import queue

from ..lib import file_stream_utils

import pprint


logger = logging.getLogger(__name__)

Chunk = collections.namedtuple("Chunk", ("filename", "data"))


class DefaultFilePolicy(object):
    def __init__(self, start_chunk_id=0):
        self._chunk_id = start_chunk_id

    def process_chunks(self, chunks):
        chunk_id = self._chunk_id
        self._chunk_id += len(chunks)
        return {"offset": chunk_id, "content": [c.data for c in chunks]}


class JsonlFilePolicy(DefaultFilePolicy):
    def process_chunks(self, chunks):
        chunk_id = self._chunk_id
        # TODO: chunk_id is getting reset on each request...
        self._chunk_id += len(chunks)
        chunk_data = []
        for chunk in chunks:
            if len(chunk.data) > util.MAX_LINE_BYTES:
                msg = "Metric data exceeds maximum size of {} ({})".format(
                    util.to_human_size(util.MAX_LINE_BYTES),
                    util.to_human_size(len(chunk.data)),
                )
                wandb.termerror(msg, repeat=False)
                util.sentry_message(msg)
            else:
                chunk_data.append(chunk.data)

        return {
            "offset": chunk_id,
            "content": chunk_data,
        }


class SummaryFilePolicy(DefaultFilePolicy):
    def process_chunks(self, chunks):
        data = chunks[-1].data
        if len(data) > util.MAX_LINE_BYTES:
            msg = "Summary data exceeds maximum size of {}. Dropping it.".format(
                util.to_human_size(util.MAX_LINE_BYTES)
            )
            wandb.termerror(msg, repeat=False)
            util.sentry_message(msg)
            return False
        return {"offset": 0, "content": [data]}


@dataclass
class Lines:
    offset: int = None
    lines: List = field(default_factory=lambda: [])
    flag: bool = False


class CRDedupeFilePolicy(DefaultFilePolicy):
    """File stream policy that removes characters that would be erased by
    carriage returns.

    This is what a terminal does. We use it for console output to reduce the
    amount of data we need to send over the network (eg. for progress bars),
    while preserving the output's appearance in the web app.

    We stream chunks (of lines) from user's output logging to online console on the dashboard.
    The term "offset" refers to the line number on this online console.

    Progress bars (like tqdm) print lines with \r, which tell the terminal to move the cursor
    back to the start of the current buffer and replace it with the current line.

    When we encounter a line with \r, we freeze its offset. Most likely,
    there will be future lines which contain \r (progress bar updates) which will replace that line.
    So we freeze the offset and update with the latest line containing \r.
    """

    def __init__(self, start_chunk_id=0):
        super(CRDedupeFilePolicy, self).__init__(start_chunk_id=start_chunk_id)
        self._prev_chunk = None

        self.global_offset = 0
        # cr refers to carriage return \r
        self.cr_stderr = Lines()
        self.cr_stdout = Lines()

        # TO DELETE
        self.NEW_FUNCTION_DELETE = True
        self.DEBUG_FN = "fs-debug-new.log"

    def split(self, chunk):
        """
        chunk: object of type Chunk with two fields: filename (str) & data (str)
        The `data` field contains the lines we want and usually contains \n or \r or both.

        Line has two possible formats:
            - "2020-08-25T20:38:36.895321 this is my line of text\nsecond line\n"
            - "ERROR 2020-08-25T20:38:36.895321 this is my line of text\nsecond line\nthird\n"

        `prefix` is either "2020-08-25T20:38:36.895321" or "ERROR 2020-08-25T20:38:36.895321"
        """
        prefix = ""
        token, rest = chunk.data.split(" ", 1)
        is_err = False
        if token == "ERROR":
            is_err = True
            prefix += token + " "
            token, rest = rest.split(" ", 1)
        prefix += token + " "
        return prefix, rest, is_err

    def new_process_chunks(self, chunks):
        with open(self.DEBUG_FN, "a") as f:
            f.write("\n################## CRDedupeFilePolicy ########\n")
            f.write(pprint.pformat(chunks))

        normal = Lines()

        for c in chunks:
            prefix, rest, _ = self.split(c)
            lines = rest.split(os.linesep)
            for line in lines:
                cr = self.cr_stderr if prefix.startswith("ERROR ") else self.cr_stdout
                if line.startswith("\r"):
                    cr.flag = True
                    # overwrite with the newest \r line
                    cr.lines = [prefix + line[1:] + "\n"]
                    if (
                        normal.offset is not None
                        and cr.offset == normal.offset + len(normal.lines) - 1
                    ):
                        normal.lines.pop()
                elif line:
                    normal.lines.append(line)
                    if not cr.flag:
                        cr.offset = self.global_offset
                    if normal.offset is None:
                        normal.offset = self.global_offset
                    self.global_offset += 1

        ret = []
        for item in [self.cr_stderr, self.cr_stdout, normal]:
            if item.lines:
                ret.append({"offset": item.offset, "content": item.lines})

        with open(self.DEBUG_FN, "a") as f:
            f.write("\n#### RET\n")
            f.write(pprint.pformat(ret))

        return ret

    def process_chunks(self, chunks):
        if self.NEW_FUNCTION_DELETE:
            return self.new_process_chunks(chunks)

        ret = []
        flag = bool(self._prev_chunk)
        chunk_id = self._chunk_id

        DEBUG_FN = "fs-debug-nobreak.log"
        with open(DEBUG_FN, "a") as f:
            f.write("\n################## CRDedupeFilePolicy ########\n")
            f.write(pprint.pformat(chunks))

        for c in chunks:
            # Line has two possible formats:
            # 1) "2020-08-25T20:38:36.895321 this is my line of text"
            # 2) "ERROR 2020-08-25T20:38:36.895321 this is my line of text"
            prefix = ""
            token, rest = c.data.split(" ", 1)
            is_err = False
            if token == "ERROR":
                is_err = True
                prefix += token + " "
                token, rest = rest.split(" ", 1)
            prefix += token + " "

            lines = rest.split(os.linesep)
            for line in lines:
                if line.startswith("\r"):
                    found = False
                    for i in range(len(ret) - 1, -1, -1):
                        if ret[i].startswith("ERROR ") == is_err:
                            ret[i] = prefix + line[1:] + "\n"
                            found = True
                            break
                    if not found:
                        if flag:
                            flag = False
                            prev_ret = self._prev_chunk["content"]
                            for i in range(len(prev_ret) - 1, -1, -1):
                                if prev_ret[i].startswith("ERROR ") == is_err:
                                    prev_ret[i] = prefix + line[1:] + "\n"
                                    found = True
                                    break
                            if found:
                                chunk_id = self._prev_chunk["offset"]
                                ret = prev_ret + ret
                            else:
                                ret.append(prefix + line[1:] + "\n")
                        else:
                            ret.append(prefix + line[1:] + "\n")
                elif line:
                    ret.append(prefix + line + "\n")

        self._chunk_id = chunk_id + len(ret)

        with open(DEBUG_FN, "a") as f:
            f.write("\n#### RET\n")
            f.write(pprint.pformat(ret))
            f.write(f"\nNew Offset: {chunk_id}\n")
            f.write(f"{self._chunk_id=}")

        ret = {"offset": chunk_id, "content": ret}
        self._prev_chunk = ret
        return ret


class BinaryFilePolicy(DefaultFilePolicy):
    def process_chunks(self, chunks):
        data = b"".join([c.data for c in chunks])
        enc = base64.b64encode(data).decode("ascii")
        self._offset += len(data)
        return {"offset": self._offset, "content": enc, "encoding": "base64"}


class FileStreamApi(object):
    """Pushes chunks of files to our streaming endpoint.

    This class is used as a singleton. It has a thread that serializes access to
    the streaming endpoint and performs rate-limiting and batching.

    TODO: Differentiate between binary/text encoding.
    """

    Finish = collections.namedtuple("Finish", ("exitcode"))
    Preempting = collections.namedtuple("Preempting", ())
    PushSuccess = collections.namedtuple("PushSuccess", ("artifact_id", "save_name"))

    HTTP_TIMEOUT = env.get_http_timeout(10)
    MAX_ITEMS_PER_PUSH = 10000

    def __init__(self, api, run_id, start_time, settings=None):
        if settings is None:
            settings = dict()
        # NOTE: exc_info is set in thread_except_body context and readable by calling threads
        self._exc_info = None
        self._settings = settings
        self._api = api
        self._run_id = run_id
        self._start_time = start_time
        self._client = requests.Session()
        self._client.auth = ("api", api.api_key)
        self._client.timeout = self.HTTP_TIMEOUT
        self._client.headers.update(
            {
                "User-Agent": api.user_agent,
                "X-WANDB-USERNAME": env.get_username(),
                "X-WANDB-USER-EMAIL": env.get_user_email(),
            }
        )
        self._file_policies = {}
        self._dropped_chunks = 0
        self._queue = queue.Queue()
        self._thread = threading.Thread(target=self._thread_except_body)
        # It seems we need to make this a daemon thread to get sync.py's atexit handler to run, which
        # cleans this thread up.
        self._thread.name = "FileStreamThread"
        self._thread.daemon = True
        self._init_endpoint()

    def _init_endpoint(self):
        settings = self._api.settings()
        settings.update(self._settings)
        self._endpoint = "{base}/files/{entity}/{project}/{run}/file_stream".format(
            base=settings["base_url"],
            entity=settings["entity"],
            project=settings["project"],
            run=self._run_id,
        )

    def start(self):
        self._init_endpoint()
        self._thread.start()

    def set_default_file_policy(self, filename, file_policy):
        """Set an upload policy for a file unless one has already been set."""
        if filename not in self._file_policies:
            self._file_policies[filename] = file_policy

    def set_file_policy(self, filename, file_policy):
        self._file_policies[filename] = file_policy

    @property
    def heartbeat_seconds(self):
        # Defaults to 30
        return self._api.dynamic_settings["heartbeat_seconds"]

    def rate_limit_seconds(self):
        run_time = time.time() - self._start_time
        if run_time < 60:
            return max(1, self.heartbeat_seconds / 15)
        elif run_time < 300:
            return max(2.5, self.heartbeat_seconds / 3)
        else:
            return max(5, self.heartbeat_seconds)

    def _read_queue(self):
        # called from the push thread (_thread_body), this does an initial read
        # that'll block for up to rate_limit_seconds. Then it tries to read
        # as much out of the queue as it can. We do this because the http post
        # to the server happens within _thread_body, and can take longer than
        # our rate limit. So next time we get a chance to read the queue we want
        # read all the stuff that queue'd up since last time.
        #
        # If we have more than MAX_ITEMS_PER_PUSH in the queue then the push thread
        # will get behind and data will buffer up in the queue.
        return util.read_many_from_queue(
            self._queue, self.MAX_ITEMS_PER_PUSH, self.rate_limit_seconds()
        )

    def _thread_body(self):
        posted_data_time = time.time()
        posted_anything_time = time.time()
        ready_chunks = []
        uploaded = set()
        finished = None
        while finished is None:
            items = self._read_queue()
            for item in items:
                if isinstance(item, self.Finish):
                    finished = item
                elif isinstance(item, self.Preempting):
                    request_with_retry(
                        self._client.post,
                        self._endpoint,
                        json={
                            "complete": False,
                            "preempting": True,
                            "dropped": self._dropped_chunks,
                            "uploaded": list(uploaded),
                        },
                    )
                    uploaded = set()
                elif isinstance(item, self.PushSuccess):
                    uploaded.add(item.save_name)
                else:
                    # item is Chunk
                    ready_chunks.append(item)

            cur_time = time.time()

            if ready_chunks and (
                finished or cur_time - posted_data_time > self.rate_limit_seconds()
            ):
                posted_data_time = cur_time
                posted_anything_time = cur_time
                self._send(ready_chunks)
                ready_chunks = []

            if cur_time - posted_anything_time > self.heartbeat_seconds:
                posted_anything_time = cur_time
                self._handle_response(
                    request_with_retry(
                        self._client.post,
                        self._endpoint,
                        json={
                            "complete": False,
                            "failed": False,
                            "dropped": self._dropped_chunks,
                            "uploaded": list(uploaded),
                        },
                    )
                )
                uploaded = set()
        # post the final close message. (item is self.Finish instance now)
        request_with_retry(
            self._client.post,
            self._endpoint,
            json={
                "complete": True,
                "exitcode": int(finished.exitcode),
                "dropped": self._dropped_chunks,
                "uploaded": list(uploaded),
            },
        )

    def _thread_except_body(self):
        # TODO: Consolidate with internal_util.ExceptionThread
        try:
            self._thread_body()
        except Exception as e:
            exc_info = sys.exc_info()
            self._exc_info = exc_info
            logger.exception("generic exception in filestream thread")
            util.sentry_exc(exc_info, delay=True)
            raise e

    def _handle_response(self, response):
        """Logs dropped chunks and updates dynamic settings"""
        if isinstance(response, Exception):
            wandb.termerror(
                "Dropped streaming file chunk (see wandb/debug-internal.log)"
            )
            logging.exception("dropped chunk %s" % response)
            self._dropped_chunks += 1
        else:
            parsed: dict = None
            try:
                parsed = response.json()
            except Exception:
                pass
            if isinstance(parsed, dict):
                limits = parsed.get("limits")
                if isinstance(limits, dict):
                    self._api.dynamic_settings.update(limits)

    def _send(self, chunks):
        # create files dict. dict of <filename: chunks> pairs where chunks is a list of
        # [chunk_id, chunk_data] tuples (as lists since this will be json).
        files = {}
        # Groupby needs group keys to be consecutive, so sort first.
        chunks.sort(key=lambda c: c.filename)
        for filename, file_chunks in itertools.groupby(chunks, lambda c: c.filename):
            file_chunks = list(file_chunks)  # groupby returns iterator
            # Specific file policies are set by internal/sender.py
            self.set_default_file_policy(filename, DefaultFilePolicy())
            files[filename] = self._file_policies[filename].process_chunks(file_chunks)
            if not files[filename]:
                del files[filename]

            """
            processed = self._file_policies[filename].process_chunks(file_chunks)
            if type(processed) is list:
                repeated[filename] = processed
            else:
                files[filename] = processed
                if not files[filename]:
                    del files[filename]

            """

        for fs in file_stream_utils.split_files(files, max_bytes=util.MAX_LINE_BYTES):
            self._handle_response(
                request_with_retry(
                    self._client.post,
                    self._endpoint,
                    json={"files": fs, "dropped": self._dropped_chunks},
                    retry_callback=self._api.retry_callback,
                )
            )
        """
        for fn in repeated:
            for data in repeated[fn]:
                for fs in file_stream_utils.split_files(
                    {fn: data}, max_bytes=util.MAX_LINE_BYTES
                ):
                    self._handle_response(
                        request_with_retry(
                            self._client.post,
                            self._endpoint,
                            json={"files": fs, "dropped": self._dropped_chunks},
                            retry_callback=self._api.retry_callback,
                        )
                    )

        """

    def stream_file(self, path):
        name = path.split("/")[-1]
        with open(path) as f:
            self._send([Chunk(name, line) for line in f])

    def enqueue_preempting(self):
        self._queue.put(self.Preempting())

    def push(self, filename, data):
        """Push a chunk of a file to the streaming endpoint.

        Arguments:
            filename: Name of file that this is a chunk of.
            chunk_id: TODO: change to 'offset'
            chunk: File data.
        """
        self._queue.put(Chunk(filename, data))

    def push_success(self, artifact_id, save_name):
        """Notification that a file upload has been successfully completed

        Arguments:
            artifact_id: ID of artifact
            save_name: saved name of the uploaded file
        """
        self._queue.put(self.PushSuccess(artifact_id, save_name))

    def finish(self, exitcode):
        """Cleans up.

        Anything pushed after finish will be dropped.

        Arguments:
            exitcode: The exitcode of the watched process.
        """
        self._queue.put(self.Finish(exitcode))
        # TODO(jhr): join on a thread which exited with an exception is a noop, clean up this path
        self._thread.join()
        if self._exc_info:
            logger.error("FileStream exception", exc_info=self._exc_info)
            # reraising the original exception, will get recaught in internal.py for the sender thread
            six.reraise(*self._exc_info)


MAX_SLEEP_SECONDS = 60 * 5


def request_with_retry(func, *args, **kwargs):
    """Perform a requests http call, retrying with exponential backoff.

    Arguments:
        func: An http-requesting function to call, like requests.post
        max_retries: Maximum retries before giving up. By default we retry 30 times in ~2 hours before dropping the chunk
        *args: passed through to func
        **kwargs: passed through to func
    """
    max_retries = kwargs.pop("max_retries", 30)
    retry_callback = kwargs.pop("retry_callback", None)
    sleep = 2
    retry_count = 0
    while True:
        try:
            response = func(*args, **kwargs)
            response.raise_for_status()
            return response
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.HTTPError,
            requests.exceptions.Timeout,
        ) as e:
            if isinstance(e, requests.exceptions.HTTPError):
                # Non-retriable HTTP errors.
                #
                # We retry 500s just to be cautious, and because the back end
                # returns them when there are infrastructure issues. If retrying
                # some request winds up being problematic, we'll change the
                # back end to indicate that it shouldn't be retried.
                if e.response is not None and e.response.status_code in {
                    400,
                    403,
                    404,
                    409,
                }:
                    return e

            if retry_count == max_retries:
                return e
            retry_count += 1
            delay = sleep + random.random() * 0.25 * sleep
            if isinstance(e, requests.exceptions.HTTPError) and (
                e.response is not None and e.response.status_code == 429
            ):
                err_str = (
                    "Filestream rate limit exceeded, retrying in {} seconds".format(
                        delay
                    )
                )
                if retry_callback:
                    retry_callback(e.response.status_code, err_str)
                logger.info(err_str)
            else:
                pass
                logger.warning(
                    "requests_with_retry encountered retryable exception: %s. func: %s, args: %s, kwargs: %s",
                    e,
                    func,
                    args,
                    kwargs,
                )
            time.sleep(delay)
            sleep *= 2
            if sleep > MAX_SLEEP_SECONDS:
                sleep = MAX_SLEEP_SECONDS
        except requests.exceptions.RequestException as e:
            error_message = "unknown error"
            try:
                error_message = response.json()["error"]  # XXX clean this up
            except Exception:
                pass
            logger.error("requests_with_retry error: {}".format(error_message))
            logger.exception(
                "requests_with_retry encountered unretryable exception: %s", e
            )
            return e
