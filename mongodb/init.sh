#!/bin/sh
set -e

# Entry point for the MongoDB deployment.
#
# mongod runs with an explicit --dbpath under /app/state, the Quix state volume
# that outlives the container. Three things follow from that:
#
#   1. The volume can be empty (first start) or provisioned root-owned, so the
#      directory has to be created and/or re-owned before mongod touches it.
#   2. Files inside it were written by a previous container. If their owner does
#      not match the UID mongod runs as, mongod can rename WiredTiger.wt (that
#      only needs write permission on the directory) but cannot open it, and
#      dies with "Operation not permitted" / Fatal assertion 28595.
#   3. A dbpath WiredTiger cannot recover cannot be repaired from here. So the
#      dbpath is configurable instead: point MONGO_DBPATH at an unused directory
#      on the same volume and mongod initialises cleanly, with no shell inside
#      the container and no manual wipe.
#
# The fix for (2) is to own the files, on every start. Do NOT renumber the
# mongodb account to match the volume: that shifts the UID out from under files
# written by earlier runs and causes exactly the failure it appears to avoid.
#
# The official mongo docker-entrypoint.sh only chowns the hardcoded /data/db and
# /data/configdb paths, never a custom --dbpath, so it cannot do this for us.
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

# Repair ownership on every start, recursively, before dropping privileges.
# Idempotent, and cheap at this data size.
if [ "$(id -u)" -eq 0 ]; then
  chown -R "$TARGET_USER:$TARGET_GROUP" "$TARGET_DIR" || {
    echo "❌ Failed to chown -R $TARGET_DIR to $TARGET_USER:$TARGET_GROUP"
    exit 1
  }
  echo "mongodb-init: $TARGET_DIR owned by $TARGET_USER:$TARGET_GROUP (recursive)"
else
  echo "⚠️  mongodb-init: not running as root (uid $(id -u)); cannot repair ownership of $TARGET_DIR"
fi

# Diagnostics: what mongod is about to see.
echo "mongodb-init: dbpath uid=$(stat -c '%u' "$TARGET_DIR") gid=$(stat -c '%g' "$TARGET_DIR"); $TARGET_USER uid=$(id -u "$TARGET_USER") gid=$(id -g "$TARGET_USER")"

# Hand over to the official entry point as the unprivileged mongodb user.
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
# Running the entry point as mongodb also skips its own chown of /data/db, which
# is irrelevant here: the chown above already covers the dbpath in use.
if [ "$(id -u)" -ne 0 ]; then
  echo "mongodb-init: already unprivileged; starting mongod without dropping privileges"
  exec docker-entrypoint.sh mongod --bind_ip_all --dbpath "$TARGET_DIR"
fi

if command -v gosu >/dev/null 2>&1; then
  exec gosu "$TARGET_USER" docker-entrypoint.sh mongod --bind_ip_all --dbpath "$TARGET_DIR"
fi

echo "⚠️  mongodb-init: gosu not found in this image; falling back to su. SIGTERM will not reach mongod, so stops will be unclean."
exec su -s /bin/sh "$TARGET_USER" -c "docker-entrypoint.sh mongod --bind_ip_all --dbpath '$TARGET_DIR'"
