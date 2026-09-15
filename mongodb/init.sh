#!/bin/sh
set -e

# Entry point for the MongoDB deployment.
#
# mongod runs with an explicit --dbpath under /app/state, the Quix state volume
# that outlives the container. Three things follow from that:
#
#   1. The volume can be empty (first start), or provisioned with an ownership
#      this container is not allowed to change, so the directory has to be
#      created and the uid mongod runs as has to be reconciled with the
#      directory's owner before mongod starts.
#   2. Files inside it were written by a previous container. If their owner does
#      not match the UID mongod runs as, mongod can rename WiredTiger.wt (that
#      only needs write permission on the directory) but cannot open it, and
#      dies with "Operation not permitted" / Fatal assertion 28595.
#   3. A dbpath WiredTiger cannot recover cannot be repaired from here. So the
#      dbpath is configurable instead: point MONGO_DBPATH at an unused directory
#      on the same volume and mongod initialises cleanly, with no shell inside
#      the container and no manual wipe.
#
# (1) and (2) are one requirement - the uid mongod runs as must own the dbpath -
# and there is more than one way to reach it, so the block below is a ladder,
# tried in order and logged by rung:
#
#   1. The dbpath is already owned by mongodb. Nothing to do.
#   2. chown -R the dbpath to mongodb. This is what a normal volume needs, and
#      it is idempotent and cheap at this data size.
#   3. chown refused: renumber the mongodb account onto the dbpath's uid/gid
#      with usermod/groupmod, then drop to it. This is what the Quix state
#      volume on the testrig cluster needs. It squashes ownership to the
#      kernel's overflow id (4294967294, i.e. (uid_t)-2), which is why the
#      directory shows up as owned by nobody and why even root's chown is
#      denied there.
#   4. Both refused: run mongod directly on the dbpath's numeric uid:gid.
#
# Rung 3 moves the account to the directory rather than the directory to the
# account, which is only safe because it is unreachable unless chown is
# impossible: a volume that refuses chown cannot be carrying files that an
# earlier run chowned to mongodb's old uid, so the renumbering has no existing
# files to strand. The reverse case - a volume where chown works and older files
# carry some other uid - is settled by rung 2 and never reaches rung 3.
#
# The official mongo docker-entrypoint.sh only chowns the hardcoded /data/db and
# /data/configdb paths, never a custom --dbpath, so it cannot do any of this for
# us. It does, however, re-exec itself under `gosu mongodb` whenever it is
# started as root - which is why rung 4 hands it a non-root uid explicitly
# rather than simply staying root: as root, the entry point would put mongod
# back on mongodb's uid and undo the reconciliation.
#
# This script never deletes data. Abandoning a dbpath is a configuration
# decision (bump MONGO_DBPATH); deleting one is not the entry point's business.
# An abandoned directory stays on the volume, inspectable, until a human removes
# it.

# The Quix state volume mount point. Anything mongod writes must live under it;
# every other path is container-local and vanishes on the next restart.
STATE_MOUNT="/app/state"

# Default dbpath. The original /app/state/mongodb was abandoned on 2026-08-18:
# repeated unclean shutdowns left it with no valid WiredTiger.turtle and the
# metadata table stranded as WiredTiger.wt.17, which mongod cannot recover in
# place. Its files are still there, untouched, if anyone wants to look.
DEFAULT_DBPATH="$STATE_MOUNT/mongodb-v2"

TARGET_USER="mongodb"
TARGET_GROUP="mongodb"

# --- resolve the dbpath ------------------------------------------------------
# Unset means "use the default". Set-but-empty means someone blanked the
# variable, which is a mistake worth failing on rather than guessing about.
if [ -n "${MONGO_DBPATH+isset}" ]; then
  TARGET_DIR="$MONGO_DBPATH"
  if [ -z "$TARGET_DIR" ]; then
    echo "❌ MONGO_DBPATH is set but empty. Set it to a directory under $STATE_MOUNT/ (default: $DEFAULT_DBPATH) or unset it."
    exit 1
  fi
else
  TARGET_DIR="$DEFAULT_DBPATH"
  echo "mongodb-init: MONGO_DBPATH not set; using default $TARGET_DIR"
fi

# --- validate the dbpath -----------------------------------------------------
# It has to be an absolute path strictly inside the state mount. A path outside
# it would appear to work and then lose the whole database on the next restart.
case "$TARGET_DIR" in
  "$STATE_MOUNT"/?*) : ;;
  *)
    echo "❌ MONGO_DBPATH must be an absolute path inside the state volume, i.e. $STATE_MOUNT/<dir>. Got: '$TARGET_DIR'"
    echo "   Anything outside $STATE_MOUNT is container-local storage and is discarded when the container restarts."
    exit 1
    ;;
esac

# Reject traversal, which could climb back out of the state mount.
case "$TARGET_DIR" in
  */../* | */..)
    echo "❌ MONGO_DBPATH must not contain '..' path segments. Got: '$TARGET_DIR'"
    exit 1
    ;;
esac

# The mount itself must exist, otherwise state is not enabled on this
# deployment and mkdir below would silently create an ephemeral directory.
if [ ! -d "$STATE_MOUNT" ]; then
  echo "❌ State volume $STATE_MOUNT is not mounted. Enable state on this deployment; mongod must not run on container-local storage."
  exit 1
fi

echo "mongodb-init: dbpath $TARGET_DIR"

# Create the data directory if it does not exist yet (first start, or a freshly
# bumped MONGO_DBPATH).
if [ ! -d "$TARGET_DIR" ]; then
  mkdir -p "$TARGET_DIR" || {
    echo "❌ Failed to create $TARGET_DIR"
    exit 1
  }
  echo "mongodb-init: created $TARGET_DIR"
fi

# --- reconcile the dbpath's owner with the uid mongod will run as ------------
# Every rung is a probe that is allowed to fail, so each one runs as an `if`
# condition or with an explicit `||` fallback. `set -e` never sees a bare
# failing command here: the ladder decides what a failure means, instead of the
# shell aborting the container on it.

DIR_UID=$(stat -c '%u' "$TARGET_DIR")
DIR_GID=$(stat -c '%g' "$TARGET_DIR")
USER_UID=$(id -u "$TARGET_USER" 2>/dev/null || true)
USER_GID=$(id -g "$TARGET_USER" 2>/dev/null || true)

echo "mongodb-init: dbpath uid=$DIR_UID gid=$DIR_GID; $TARGET_USER uid=${USER_UID:--} gid=${USER_GID:--}"

# Rung 3, factored out because it is two commands with different consequences.
# It returns non-zero - fall through to rung 4 - only when the uid could not be
# moved: usermod may be missing from a non-official image, and some shadow-utils
# builds reject a uid this large outright. Neither is a reason to kill the
# container, unlike the reference implementation in TestManager, which exits.
renumber_target_account() {
  if ! command -v usermod >/dev/null 2>&1; then
    echo "⚠️  mongodb-init: usermod is not available in this image."
    return 1
  fi
  # -o: the dbpath's uid may already belong to another account in /etc/passwd,
  # which is not a reason to refuse.
  if ! usermod -o -u "$DIR_UID" "$TARGET_USER"; then
    echo "⚠️  mongodb-init: usermod -u $DIR_UID $TARGET_USER was refused."
    return 1
  fi
  # The gid is secondary: once the uid matches, the directory's owner
  # permissions already cover everything mongod does, so a failed groupmod is
  # worth a warning and no more.
  if ! command -v groupmod >/dev/null 2>&1; then
    echo "⚠️  mongodb-init: groupmod is not available; continuing with the uid match alone."
  elif ! groupmod -o -g "$DIR_GID" "$TARGET_GROUP"; then
    echo "⚠️  mongodb-init: groupmod -g $DIR_GID $TARGET_GROUP was refused; continuing with the uid match alone."
  fi
  return 0
}

# Set to a gosu user-spec as soon as a rung succeeds. Empty means rung 4: no
# account could be matched to the dbpath, so mongod runs on its raw uid:gid.
RUN_AS=""

if [ -z "$USER_UID" ] || [ -z "$USER_GID" ]; then
  echo "⚠️  mongodb-init: rung 4 - this image has no '$TARGET_USER' account to reconcile."
elif [ "$DIR_UID" -eq "$USER_UID" ]; then
  RUN_AS="$TARGET_USER"
  echo "mongodb-init: rung 1 - $TARGET_DIR is already owned by $TARGET_USER (uid $DIR_UID); nothing to reconcile."
elif [ "$(id -u)" -ne 0 ]; then
  echo "⚠️  mongodb-init: rung 4 - running as uid $(id -u), not root; cannot chown $TARGET_DIR or renumber $TARGET_USER."
# chown's own stderr is left visible on purpose: when it fails, its message
# names the reason, which beats any diagnosis guessed here.
elif chown -R "$TARGET_USER:$TARGET_GROUP" "$TARGET_DIR"; then
  RUN_AS="$TARGET_USER"
  echo "mongodb-init: rung 2 - chowned $TARGET_DIR to $TARGET_USER:$TARGET_GROUP (recursive)."
elif renumber_target_account; then
  RUN_AS="$TARGET_USER"
  echo "mongodb-init: rung 3 - chown was refused, so $TARGET_USER was renumbered onto the dbpath (uid $DIR_UID, gid $DIR_GID) instead."
else
  echo "⚠️  mongodb-init: rung 4 - $TARGET_DIR cannot be chowned and $TARGET_USER cannot be renumbered onto it."
fi

# What mongod is about to see, after reconciliation. This line is what
# identified the squashed-ownership failure in the first place; keep it.
echo "mongodb-init: dbpath uid=$(stat -c '%u' "$TARGET_DIR") gid=$(stat -c '%g' "$TARGET_DIR"); $TARGET_USER uid=$(id -u "$TARGET_USER" 2>/dev/null || echo -) gid=$(id -g "$TARGET_USER" 2>/dev/null || echo -)"

# --- hand over to the official entry point -----------------------------------
#
# gosu, not `su -c`: gosu execs the target directly, so PID 1 becomes
# docker-entrypoint.sh and then mongod itself, with no shell in between. The
# SIGTERM Kubernetes sends on pod termination therefore reaches mongod and
# WiredTiger checkpoints and closes its files. Under `su -c` the signal went to
# the intervening shell, mongod was SIGKILLed at the end of the grace period,
# and every stop was an unclean shutdown - which is how the previous dbpath was
# destroyed. gosu ships in the official mongo image (/usr/local/bin/gosu);
# docker-entrypoint.sh uses it itself to drop privileges when started as root.
#
# Dropping privileges here is also what pins the uid mongod ends up on. Started
# as root, docker-entrypoint.sh re-execs itself under `gosu mongodb` and would
# override the ladder's choice; started non-root, it runs mongod as whoever it
# inherited. So every branch below that can drop does so, and only the one where
# gosu is missing hands the entry point a root process and lets it pick the uid.
# Dropping here also skips the entry point's own chown of /data/db, which is
# irrelevant: /data/db is not the dbpath.

if [ "$(id -u)" -ne 0 ]; then
  echo "mongodb-init: already unprivileged (uid $(id -u)); starting mongod without dropping privileges."
  exec docker-entrypoint.sh mongod --bind_ip_all --dbpath "$TARGET_DIR"
fi

if ! command -v gosu >/dev/null 2>&1; then
  echo "⚠️  mongodb-init: gosu not found in this image; starting mongod as root and leaving the uid to docker-entrypoint.sh, which drops to $TARGET_USER by itself."
  exec docker-entrypoint.sh mongod --bind_ip_all --dbpath "$TARGET_DIR"
fi

if [ -n "$RUN_AS" ]; then
  echo "mongodb-init: starting mongod as $RUN_AS (uid $(id -u "$RUN_AS"))."
  exec gosu "$RUN_AS" docker-entrypoint.sh mongod --bind_ip_all --dbpath "$TARGET_DIR"
fi

# Rung 4. gosu accepts a numeric user-spec for ids that have no /etc/passwd
# entry, which is exactly what is needed when the volume's enforced owner is the
# kernel's overflow id rather than a real account.
echo "mongodb-init: starting mongod as uid:gid $DIR_UID:$DIR_GID, the dbpath's own owner."
exec gosu "$DIR_UID:$DIR_GID" docker-entrypoint.sh mongod --bind_ip_all --dbpath "$TARGET_DIR"
