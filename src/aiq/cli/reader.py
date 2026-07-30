from __future__ import annotations

import argparse
import sys

from aiq.cli._protocol import _emit, _scope, _scope_parser, _single_line
from aiq.queue import (
    acquire_reader_lease,
    read_reader_lease,
    release_reader_lease,
)


def _reader_status(arguments: argparse.Namespace) -> int:
    scope = _scope(arguments)
    lease = read_reader_lease(
        scope,
        reader_id=arguments.effective_config.reader,
    )
    if arguments.json:
        _emit({"reader": lease, "scope": scope.to_dict()}, as_json=True)
        return 0
    print(
        f"{lease['status']}\t{_single_line(lease['reader_id'] or '-')}\t"
        f"{_single_line(lease['owner_id'] or '-')}\t"
        f"{lease['expires_at'] or '-'}"
    )
    return 0


def _reader_acquire(arguments: argparse.Namespace) -> int:
    config = arguments.effective_config
    result = acquire_reader_lease(
        _scope(arguments),
        owner_id=config.owner,
        reader_id=config.reader,
        # --lease-seconds names the reader lease here, not an item lease;
        # this command leases no message and no task.
        lease_seconds=(
            arguments.lease_seconds
            if arguments.lease_seconds is not None
            else config.reader_lease_seconds
        ),
    )
    if arguments.json:
        _emit(result, as_json=True)
        return 0
    _emit(
        {"status": result["status"], "acquired": result["acquired"]},
        as_json=False,
    )
    return 0


def _reader_release(arguments: argparse.Namespace) -> int:
    reader_id = arguments.effective_config.reader
    result = release_reader_lease(
        _scope(arguments),
        reader_id=reader_id,
        force=arguments.force,
    )
    # Say plainly when there was nothing of this caller's to give back.
    # A release that matched no lease records no declaration, so nothing
    # downstream -- the completion gate above all -- will read it as one,
    # and a caller told only "released" would stop expecting a signal
    # that was never written. The usual cause is that this session cannot
    # prove it holds the role, so the line names the identity that tried.
    if result["status"] == "not_held":
        print(
            f'aiq: nothing to release: reader "{reader_id}" does not hold '
            "the reader role, so no release was recorded; check who holds "
            "it: aiq reader status",
            file=sys.stderr,
        )
    # A release that succeeded but recorded no declaration is the one
    # outcome a bounded run can misread as success and then stall on. It
    # happens under an explicitly configured `--reader` or `AIQ_READER`:
    # such an identity may name any session on any host, so the lease
    # stores no holder locator, and without one nothing can prove the
    # release was *this session's*. The role really is handed back, so
    # this is not an error -- but `reader.released_by_self` stays false,
    # so a completion gate goes on blocking, and a caller told only
    # "released" would wait for a signal that was never written.
    if result["status"] in ("released", "already_released") and not result[
        "declared"
    ]:
        print(
            f'aiq: released the reader role as configured reader "{reader_id}"'
            ", but recorded no completion signal: a configured reader "
            "identity stores no session locator, so this release cannot be "
            "proved to be this session's and a completion gate keeps "
            "blocking; to end a bounded run, let AIQ derive the reader "
            "identity and export a session identity instead: AIQ_SESSION_ID",
            file=sys.stderr,
        )
    # Breaking somebody else's live lease is an operator act with a
    # consequence the caller must see: the former holder was not asked
    # and may still be draining. Say so, and say that nothing was
    # declared -- a forced break is deliberately not a completion signal
    # for anyone.
    if result["status"] == "forced":
        print(
            "aiq: forced: broke the live reader lease held by reader "
            f'"{_single_line(result["reader"]["reader_id"] or "-")}"; the '
            "role is now free, no release was recorded for any session, "
            "and that holder may still be draining this queue",
            file=sys.stderr,
        )
    # Giving the role back is not settling the work: release leaves every
    # per-item claim in place. Warn rather than refuse, because release is
    # a total, replayable declaration and a handoff mid-item is legitimate;
    # the completion gate is what actually stops such a session. One line,
    # on stderr, so machine callers reading stdout are unaffected. A force
    # is exempt: those claims are not this caller's obligation to settle,
    # they are the broken holder's.
    held = result["claims_held"]
    if held and result["status"] != "forced":
        print(
            f"aiq: released the reader role while still holding {held} "
            f"active claim{'' if held == 1 else 's'}; settle or release "
            "them before stopping: aiq claim list --status active",
            file=sys.stderr,
        )
    if arguments.json:
        _emit(result, as_json=True)
        return 0
    _emit(
        {
            "status": result["status"],
            "released": result["released"],
            "declared": result["declared"],
            "replayed": result["replayed"],
            "claims_held": held,
        },
        as_json=False,
    )
    return 0


def register(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    reader = subparsers.add_parser(
        "reader",
        description=(
            "Inspect or hold the scope's single reader role. Many "
            "sessions may ingest and enqueue; one at a time may drain."
        ),
    )
    reader_commands = reader.add_subparsers(
        dest="reader_command",
        required=True,
    )
    reader_status = _scope_parser(reader_commands, "status")
    reader_status.set_defaults(handler=_reader_status)
    reader_acquire = _scope_parser(reader_commands, "acquire")
    reader_acquire.add_argument("--reader")
    reader_acquire.add_argument("--owner")
    reader_acquire.add_argument("--lease-seconds", type=int)
    reader_acquire.set_defaults(handler=_reader_acquire)
    reader_release = _scope_parser(reader_commands, "release")
    reader_release.add_argument("--reader")
    reader_release.add_argument(
        "--force",
        action="store_true",
        help=(
            "break a live lease this session cannot prove it holds; "
            "frees the role and records no release for any session"
        ),
    )
    reader_release.set_defaults(handler=_reader_release)
