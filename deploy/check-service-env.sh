#!/bin/sh
# Validate a private service environment, or initialize it by explicit choice.
# The arguments allow an isolated fixture to exercise the same check as the installer.
set -eu
[ "$#" -eq 5 ] || { echo 'Usage: check-service-env.sh TARGET EXAMPLE UID GID yes|no' >&2; exit 1; }
target=$1
example=$2
expected_owner=$3:$4
initialize=$5
case "$initialize" in yes|no) ;; *) echo 'Invalid initialization choice' >&2; exit 1;; esac

validate_target() {
    if [ -L "$target" ]; then
        echo 'Unsafe service environment: symlink' >&2
        exit 1
    fi
    [ -f "$target" ] || { echo 'Unsafe service environment: not a regular file' >&2; exit 1; }
    [ "$(stat -c '%u:%g' -- "$target")" = "$expected_owner" ] || {
        echo 'Unsafe service environment: unexpected owner' >&2; exit 1;
    }
    case "$(stat -c '%a' -- "$target")" in
        600|640) ;;
        *) echo 'Unsafe service environment: permissions must be 0600 or 0640' >&2; exit 1;;
    esac
}

if [ -L "$target" ] || [ -e "$target" ]; then
    validate_target
    exit 0
fi

[ "$initialize" = yes ] || { echo 'Service environment is missing; pass --init-service-env for first install' >&2; exit 1; }
[ -f "$example" ] && [ ! -L "$example" ] || { echo 'Service environment example is unavailable' >&2; exit 1; }
umask 077
temporary=$(mktemp "${target}.tmp.XXXXXXXX")
trap 'rm -f -- "$temporary"' EXIT HUP INT TERM
cat -- "$example" > "$temporary"
chown "$expected_owner" "$temporary"
chmod 0640 "$temporary"
# GNU ln -T treats the target as a filename, including when a directory appears.
ln -T -- "$temporary" "$target" || { echo 'Service environment appeared during initialization; no overwrite' >&2; exit 1; }
validate_target
[ "$(stat -c '%d:%i' -- "$target")" = "$(stat -c '%d:%i' -- "$temporary")" ] || {
    echo 'Service environment changed during initialization' >&2; exit 1;
}
