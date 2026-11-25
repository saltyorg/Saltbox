#!/usr/bin/env bash
# Tests for update.sh with fake sb, uv and gh. All versions are invented.

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
update="$script_dir/update.sh"
repo_root=$(cd -- "$script_dir/../../.." && pwd)
root=$(mktemp -d)
trap 'rm -rf -- "$root"' EXIT

repo=example/Saltbox
sb_repo=example/sb
now=1900000000
failures=0
passes=0

fail() {
    echo "not ok - $case_name: $*" >&2
    exit 1
}

# ---------------------------------------------------------------- fakes

write_fake_gh() {
    cat > "$1/gh" <<'GH'
#!/usr/bin/env bash
set -euo pipefail
state=${FAKE_GH_STATE:?}
printf '%s\n' "$*" >> "$state/gh-calls"
not_found() { echo "HTTP 404: Not Found ($1)" >&2; exit 1; }
write() { printf '%s\n' "$*" >> "$state/gh-writes"; }
case $1 in
    api)
        shift
        method=GET path="" jq_expr="" fields=()
        : > "$state/input"
        while (($#)); do
            case $1 in
                -X) method=$2; shift 2 ;;
                --jq) jq_expr=$2; shift 2 ;;
                --input) cat "$2" > "$state/input"; shift 2 ;;
                -f | -F) fields+=("$2"); shift 2 ;;
                -H) shift 2 ;;
                *) path=$1; shift ;;
            esac
        done
        field() {
            local item
            for item in "${fields[@]}"; do
                [[ $item == "$1="* ]] && printf '%s\n' "${item#*=}"
            done
            return 0
        }
        respond() {
            if [[ -n $jq_expr ]]; then jq -r "$jq_expr"; else cat; fi
        }
        case "$method $path" in
            "GET repos/"*"/releases/latest")
                name=${path#repos/}
                name=${name%/releases/latest}
                file="$state/releases/${name//\//_}.json"
                [[ -f $file ]] || not_found "$path"
                respond < "$file"
                ;;
            "GET repos/$FAKE_REPO/git/ref/heads/master")
                jq -n --arg sha "$(cat "$state/master-sha")" '{object: {sha: $sha}}' | respond
                ;;
            "GET repos/$FAKE_REPO/git/ref/heads/python-toolchain")
                [[ -f "$state/branch" ]] || not_found "$path"
                jq -n --arg sha "$(cat "$state/branch")" '{object: {sha: $sha}}' | respond
                ;;
            "GET repos/$FAKE_REPO/git/commits/"*)
                file="$state/commits/${path##*/}.json"
                [[ -f $file ]] || not_found "$path"
                respond < "$file"
                ;;
            "POST repos/$FAKE_REPO/git/trees")
                write "create tree"
                sha=$(sha1sum < "$state/input" | cut -c1-40)
                cp "$state/input" "$state/trees/$sha.json"
                jq -n --arg sha "$sha" '{sha: $sha}' | respond
                ;;
            "POST repos/$FAKE_REPO/git/commits")
                write "create commit"
                count=$(find "$state/commits" -type f | wc -l)
                sha=$( { cat "$state/input"; echo "$count"; } | sha1sum | cut -c1-40)
                verified=true
                [[ ! -f "$state/unsigned" ]] || verified=false
                jq --arg sha "$sha" --argjson verified "$verified" '{
                    sha: $sha, message: .message, tree: {sha: .tree},
                    parents: [.parents[] | {sha: .}], verification: {verified: $verified}
                }' "$state/input" > "$state/commits/$sha.json"
                respond < "$state/commits/$sha.json"
                ;;
            "PATCH repos/$FAKE_REPO/git/refs/heads/python-toolchain")
                [[ -f "$state/branch" ]] || not_found "$path"
                [[ $(field force) == true ]] || { echo "not a fast-forward" >&2; exit 1; }
                write "move branch"
                field sha > "$state/branch"
                echo '{}'
                ;;
            "POST repos/$FAKE_REPO/git/refs")
                [[ ! -f "$state/branch" ]] || { echo "Reference already exists" >&2; exit 1; }
                [[ $(field ref) == refs/heads/python-toolchain ]] || exit 1
                write "create branch"
                field sha > "$state/branch"
                echo '{}'
                ;;
            "GET repos/$FAKE_REPO/pulls?"*)
                [[ $path == *"head=${FAKE_REPO%%/*}:python-toolchain"* && $path == *"state=open"* ]] || exit 1
                jq '[.[] | select(.state == "open")]' "$state/pulls.json" | respond
                ;;
            "POST repos/$FAKE_REPO/pulls")
                write "open pull request"
                number=$(jq '([.[].number] | max // 40) + 1' "$state/pulls.json")
                jq --argjson n "$number" --slurpfile pr "$state/input" \
                    '. + [$pr[0] + {number: $n, state: "open"}]' "$state/pulls.json" > "$state/pulls.new"
                mv "$state/pulls.new" "$state/pulls.json"
                jq -n --argjson n "$number" '{number: $n}' | respond
                ;;
            "PATCH repos/$FAKE_REPO/pulls/"*)
                write "update pull request ${path##*/}"
                jq --argjson n "${path##*/}" --slurpfile pr "$state/input" \
                    'map(if .number == $n then . + $pr[0] else . end)' "$state/pulls.json" > "$state/pulls.new"
                mv "$state/pulls.new" "$state/pulls.json"
                echo '{}'
                ;;
            *)
                echo "fake gh: unexpected api call: $method $path" >&2
                exit 9
                ;;
        esac
        ;;
    release)
        [[ $2 == download ]] || exit 9
        tag=$3
        shift 3
        name="" dir="" patterns=()
        while (($#)); do
            case $1 in
                --repo) name=$2; shift 2 ;;
                --pattern) patterns+=("$2"); shift 2 ;;
                --dir) dir=$2; shift 2 ;;
                *) exit 9 ;;
            esac
        done
        assets="$state/assets/${name//\//_}/$tag"
        [[ -d $assets ]] || not_found "release $tag"
        found=0
        for file in "$assets"/*; do
            for pattern in "${patterns[@]}"; do
                # shellcheck disable=SC2053 # gh matches --pattern as a glob.
                if [[ ${file##*/} == $pattern ]]; then
                    cp "$file" "$dir/"
                    found=1
                fi
            done
        done
        ((found)) || { echo "no assets match the file pattern" >&2; exit 1; }
        ;;
    pr)
        [[ $2 == merge && " $* " == *" --auto "* && " $* " == *" --squash "* ]] || exit 9
        [[ ! -f "$state/automerge-disallowed" ]] || { echo "auto-merge is not allowed" >&2; exit 1; }
        write "enable auto-merge $3"
        ;;
    *)
        echo "fake gh: unexpected command: $*" >&2
        exit 9
        ;;
esac
GH
    chmod 0755 "$1/gh"
}

# Fake uv. It also records any call that is not isolated.
write_fake_uv() {
    local file=$1 version=$2
    cat > "$file" <<'UV'
#!/usr/bin/env bash
set -euo pipefail
version=@VERSION@
state=${FAKE_UV_STATE:?}
printf '%s\n' "$*" >> "$state/uv-calls"
violation() { printf '%s: %s\n' "$1" "$*" >> "$state/isolation-violations"; }
[[ ${UV_NO_CONFIG:-} == 1 ]] || violation "UV_NO_CONFIG unset"
for variable in HOME UV_CACHE_DIR UV_PYTHON_INSTALL_DIR TMPDIR; do
    [[ ${!variable:-} == "$FAKE_TEMP_ROOT"/* ]] || violation "$variable outside the job temp dir"
done
[[ $HOME != "$FAKE_REAL_HOME" ]] || violation "real HOME"
[[ -z ${XDG_CONFIG_HOME:-} ]] || violation "XDG_CONFIG_HOME set"

fake_python() {
    printf '#!/usr/bin/env bash\necho "Python %s"\n' "$2" > "$1"
    chmod 0755 "$1"
}

case "$1 ${2:-}" in
    "--version ")
        echo "uv $version (fake 2000-01-01)"
        ;;
    "python list")
        [[ $3 == cpython@* && " $* " == *" --only-downloads "* && " $* " == *" --output-format json "* ]] || exit 3
        cat "$state/python-list.json"
        ;;
    "python install")
        target=${*: -1}
        mkdir -p "$UV_PYTHON_INSTALL_DIR/cpython-$target/bin"
        fake_python "$UV_PYTHON_INSTALL_DIR/cpython-$target/bin/python3" "$target"
        ;;
    "python find")
        target=${*: -1}
        bin="$UV_PYTHON_INSTALL_DIR/cpython-$target/bin/python3"
        [[ -x $bin ]] || { echo "No interpreter found" >&2; exit 2; }
        echo "$bin"
        ;;
    "venv "*)
        target=${*: -1}
        mkdir -p "$target/bin"
        cp "$3" "$target/bin/python"
        for tool in ansible certbot apprise; do
            printf '#!/usr/bin/env bash\n[[ ! -f %q ]] || { echo "%s: broken" >&2; exit 1; }\necho "%s 0.0.1"\n' \
                "$state/preflight-fail-$tool" "$tool" "$tool" > "$target/bin/$tool"
            chmod 0755 "$target/bin/$tool"
        done
        ;;
    "pip compile")
        original=("$@")
        shift 2
        python="" output="" custom="" floor="" exempt=() inputs=()
        while (($#)); do
            case $1 in
                --python-version) python=$2; shift 2 ;;
                --output-file) output=$2; shift 2 ;;
                --custom-compile-command) custom=$2; shift 2 ;;
                --exclude-newer) floor=$2; shift 2 ;;
                --exclude-newer-package) exempt+=("$2"); shift 2 ;;
                --generate-hashes) shift ;;
                -*) echo "unexpected flag $1" >&2; exit 3 ;;
                *) inputs+=("$1"); shift ;;
            esac
        done
        [[ -f ${inputs[0]} && -f $output ]] || { echo "compile needs the input and master's lock" >&2; exit 3; }
        printf 'python=%s floor=%s exempt=%s start=%s\n' "$python" "$floor" "${exempt[*]}" \
            "$(sha256sum < "$output" | cut -c1-64)" >> "$state/compiles"
        header=${custom:-"uv ${original[*]}"}
        {
            echo "# This file was autogenerated by uv via the following command:"
            echo "#    $header"
            echo "alpha-pkg==1.0.0 \\"
            echo "    --hash=sha256:$(printf 'alpha %s' "$python" | sha256sum | cut -c1-64)"
            echo "    # via -r ${inputs[0]}"
            if [[ -f "$state/repro-drift" && -z $floor ]]; then
                echo "gamma-pkg==3.0.0 \\"
                echo "    --hash=sha256:$(printf 'gamma' | sha256sum | cut -c1-64)"
            fi
        } > "$output"
        ;;
    "pip sync")
        [[ " $* " == *" --require-hashes "* ]] || { echo "hashes not required" >&2; exit 3; }
        ;;
    "pip check")
        ;;
    *)
        echo "fake uv: unexpected command: $*" >&2
        exit 3
        ;;
esac
UV
    sed -i "s/@VERSION@/$version/" "$file"
    chmod 0755 "$file"
}

write_fake_sb() {
    local file=$1 log=$2 json=$3
    {
        echo '#!/usr/bin/env bash'
        printf 'log=%q json=%q\n' "$log" "$json"
        cat <<'SB'
printf '%s|%s\n' "$PWD" "$*" >> "$log"
[[ "$*" == "version --json" ]] || exit 4
printf '%s\n' "$json"
SB
    } > "$file"
    chmod 0755 "$file"
}

# ---------------------------------------------------------------- fixtures

# setup <python pin> <uv pin> <sb uv> <newest-patches...>
# Builds a master checkout, an sb release carrying <sb uv>, a uv release
# for every uv version the case may target, and uv's download list.
setup() {
    local python_pin=$1 uv_pin=$2 sb_uv=$3
    shift 3
    case_dir="$root/case-$case_number"
    state="$case_dir/state"
    checkout="$case_dir/saltbox"
    result="$case_dir/result"
    mkdir -p "$state"/{releases,commits,trees} "$case_dir/bin" "$case_dir/temp" "$checkout/requirements"
    echo '[]' > "$state/pulls.json"
    write_fake_gh "$case_dir/bin"

    printf '%s\n' "$python_pin" > "$checkout/.python-version"
    printf '%s\n' "$uv_pin" > "$checkout/.uv-version"
    printf 'alpha-pkg==1.0.0\n' > "$checkout/requirements/requirements-saltbox.in"
    cat > "$checkout/requirements/requirements-saltbox.txt" <<'LOCK'
# This file was autogenerated by uv via the following command:
#    uv pip compile --generate-hashes
alpha-pkg==1.0.0 \
    --hash=sha256:0000000000000000000000000000000000000000000000000000000000000001
    # via -r requirements/requirements-saltbox.in
beta_pkg==2.0.0 \
    --hash=sha256:0000000000000000000000000000000000000000000000000000000000000002
    # via alpha-pkg
LOCK
    git -C "$checkout" init --quiet --initial-branch=master
    git -C "$checkout" add .
    git -C "$checkout" -c user.name=fixture -c user.email=fixture@example.invalid \
        -c commit.gpgsign=false commit --quiet -m 'test: fixture'
    git -C "$checkout" rev-parse HEAD > "$state/master-sha"
    base=$(cat "$state/master-sha")
    jq -n --arg sha "$base" '{sha: $sha, tree: {sha: "1111111111111111111111111111111111111111"}, parents: []}' \
        > "$state/commits/$base.json"

    sb_release '{"version":"9.9.0","git_commit":"abc1234","uv_version":"'"$sb_uv"'"}'
    local uv
    for uv in "$uv_pin" "$sb_uv"; do
        uv_release "$uv"
    done
    python_list "$@"
}

sb_release() {
    local assets="$state/assets/${sb_repo//\//_}/9.9.0" digest
    mkdir -p "$assets"
    write_fake_sb "$assets/sb_linux_amd64" "$state/sb-calls" "$1"
    digest=${FAKE_SB_DIGEST:-$(sha256sum < "$assets/sb_linux_amd64" | cut -c1-64)}
    jq -n --arg digest "sha256:$digest" '{
        tag_name: "9.9.0", draft: false, prerelease: false,
        assets: [
          {name: "sb_linux_arm64", digest: "sha256:\("0" * 64)"},
          {name: "sb_linux_amd64", digest: $digest}
        ]}' > "$state/releases/${sb_repo//\//_}.json"
}

uv_release() {
    local version=$1 assets="$state/assets/astral-sh_uv/$1" build
    [[ ! -d $assets ]] || return 0
    build="$case_dir/uv-build/$version/uv-x86_64-unknown-linux-gnu"
    mkdir -p "$assets" "$build"
    write_fake_uv "$build/uv" "$version"
    tar --create --gzip --file "$assets/uv-x86_64-unknown-linux-gnu.tar.gz" \
        --directory "${build%/*}" uv-x86_64-unknown-linux-gnu
    (cd "$assets" && sha256sum uv-x86_64-unknown-linux-gnu.tar.gz > uv-x86_64-unknown-linux-gnu.tar.gz.sha256)
}

# uv's download list: the given patches plus builds the script must ignore.
python_list() {
    local versions
    versions=$(printf '%s\n' "$@" | jq -R . | jq -s .)
    jq -n --argjson versions "$versions" '
        def entry($v; $os; $arch; $libc; $variant):
          ($v | split(".") | map(tonumber)) as $p
          | {key: "cpython-\($v)-\($os)-\($arch)-\($libc)", version: $v,
             version_parts: {major: $p[0], minor: $p[1], patch: $p[2]},
             path: null, os: $os, arch: $arch, libc: $libc, variant: $variant,
             implementation: "cpython"};
        [ $versions[] | entry(.; "linux"; "x86_64"; "gnu"; "default") ]
        + [ entry("3.12.98"; "linux"; "aarch64"; "gnu"; "default"),
            entry("3.12.97"; "linux"; "x86_64"; "musl"; "default"),
            entry("3.12.96"; "linux"; "x86_64"; "gnu"; "freethreaded"),
            entry("3.11.95"; "linux"; "x86_64"; "gnu"; "default"),
            entry("3.14.94"; "linux"; "x86_64"; "gnu"; "default") ]
        + [ {key: "cpython-3.12.99rc1-linux-x86_64-gnu", version: "3.12.99rc1",
             version_parts: {major: 3, minor: 12, patch: 99}, os: "linux", arch: "x86_64",
             libc: "gnu", variant: "default", implementation: "cpython"} ]
    ' > "$state/python-list.json"
}

# Run the workflow's two steps the way python-toolchain.yml does.
run_workflow() {
    local output="$case_dir/github-output"
    : > "$output"
    (
        cd "$checkout"
        export PATH="$case_dir/bin:$PATH" FAKE_GH_STATE="$state" FAKE_UV_STATE="$state" FAKE_REPO="$repo"
        export FAKE_TEMP_ROOT="$case_dir/temp" FAKE_REAL_HOME="$HOME"
        export RUNNER_TEMP="$case_dir/temp" GITHUB_OUTPUT="$output" GH_TOKEN=fake-read-token
        export SB_RELEASE_REPO="$sb_repo" TOOLCHAIN_NOW="$now"
        [[ $(command -v gh) == "$case_dir/bin/gh" ]] || { echo "the fake gh is not first on PATH" >&2; exit 1; }
        "$update" prepare "$result" || exit 1
        if grep -qx 'changed=true' "$output"; then
            [[ -z ${BETWEEN_STEPS:-} ]] || eval "$BETWEEN_STEPS"
            GH_TOKEN=fake-app-token GITHUB_REPOSITORY="$repo" "$update" publish "$result" || exit 1
        fi
    ) > "$case_dir/log" 2>&1
}

# ---------------------------------------------------------------- assertions

expect_success() {
    run_workflow || { cat "$case_dir/log" >&2; fail "workflow failed"; }
}

expect_failure() {
    if run_workflow; then
        cat "$case_dir/log" >&2
        fail "workflow succeeded, expected: $1"
    fi
    grep -qF -- "$1" "$case_dir/log" || { cat "$case_dir/log" >&2; fail "missing message: $1"; }
}

expect_no_pr() {
    [[ ! -s "$state/gh-writes" ]] || fail "unexpected GitHub writes: $(tr '\n' ';' < "$state/gh-writes")"
}

expect_isolated() {
    [[ ! -s "$state/isolation-violations" ]] || fail "uv ran unisolated: $(head -n 3 "$state/isolation-violations")"
}

expect_pr() {
    local python=$1 uv=$2 summary=$3 tree_file
    [[ $(jq '[.[] | select(.state == "open")] | length' "$state/pulls.json") -eq 1 ]] || fail "expected one open PR"
    jq -e --arg s "$summary" '.[0].body | startswith($s + "\n")' "$state/pulls.json" > /dev/null ||
        fail "PR body does not start with '$summary': $(jq -r '.[0].body' "$state/pulls.json")"
    jq -e '.[0].title == "chore(deps): update python toolchain" and .[0].head == "python-toolchain" and .[0].base == "master"' \
        "$state/pulls.json" > /dev/null || fail "wrong PR title, head or base"
    commit=$(cat "$state/branch")
    jq -e --arg base "$base" '(.parents | map(.sha)) == [$base]' "$state/commits/$commit.json" > /dev/null ||
        fail "branch commit is not one commit on master"
    tree_file="$state/trees/$(jq -r .tree.sha "$state/commits/$commit.json").json"
    jq -e --arg python "$python" --arg uv "$uv" '
        .base_tree == "1111111111111111111111111111111111111111"
        and ([.tree[].path] == [".python-version", ".uv-version", "requirements/requirements-saltbox.txt"])
        and .tree[0].content == ($python + "\n") and .tree[1].content == ($uv + "\n")
        and (.tree[2].content | contains("uv pip compile --python-version " + $python + " --generate-hashes --output-file requirements/requirements-saltbox.txt requirements/requirements-saltbox.in"))
    ' "$tree_file" > /dev/null || fail "commit does not change exactly the three pinned files as expected"
    grep -qx "enable auto-merge $(jq -r '.[0].number' "$state/pulls.json")" "$state/gh-writes" ||
        fail "auto-merge not enabled"
}

expect_compile() {
    local python=$1 floor
    floor=$(date -u -d "@$((now - 7 * 24 * 60 * 60))" +%Y-%m-%dT%H:%M:%SZ)
    grep -qx "python=$python floor=$floor exempt=alpha-pkg=false beta_pkg=false start=$(sha256sum < "$checkout/requirements/requirements-saltbox.txt" | cut -c1-64)" \
        "$state/compiles" || fail "age-floor compile not run as expected: $(head -n 1 "$state/compiles" 2> /dev/null)"
    grep -q "^python=$python floor= exempt= " "$state/compiles" || fail "CI's reproducibility compile did not run"
}

expect_uv_used() {
    grep -qx -- "--version" "$state/uv-calls" || fail "uv never ran"
    grep -qF "target uv $1" "$case_dir/log" || fail "target uv is not $1"
}

expect_sb_ran_once() {
    [[ $(wc -l < "$state/sb-calls") -eq 1 ]] || fail "sb ran more than once"
    grep -q "^$case_dir/temp/.*|version --json$" "$state/sb-calls" || fail "sb ran outside the job temp dir or with other arguments"
}

run_case() {
    local status
    case_name=$1
    case_number=$((passes + failures + 1))
    shift
    set +e
    (
        set -e
        "$@"
    )
    status=$?
    set -e
    if ((status == 0)); then
        echo "ok - $case_name"
        passes=$((passes + 1))
    else
        failures=$((failures + 1))
    fi
}

# ---------------------------------------------------------------- cases

nothing_newer() {
    setup 3.12.40 0.91.0 0.91.0 3.12.38 3.12.40
    expect_success
    grep -qx 'changed=false' "$case_dir/github-output" || fail "changed output is not false"
    [[ ! -s "$state/compiles" ]] || fail "compiled without a change"
    expect_no_pr
    expect_sb_ran_once
    expect_isolated
}

sb_uv_older_than_pin() {
    setup 3.12.40 0.91.2 0.91.0 3.12.40
    expect_success
    grep -qF "target uv 0.91.2" "$case_dir/log" || fail "uv went down"
    expect_no_pr
}

patch_only() {
    setup 3.12.40 0.91.0 0.91.0 3.12.40 3.12.41
    expect_success
    expect_compile 3.12.41
    expect_pr 3.12.41 0.91.0 "Python 3.12.40 → 3.12.41"
    jq -e '.[0].body | contains("uv 0.91") | not' "$state/pulls.json" > /dev/null || fail "body names an unchanged uv"
    expect_sb_ran_once
    expect_isolated
}

uv_only() {
    setup 3.12.40 0.91.0 0.91.1 3.12.40
    expect_success
    expect_uv_used 0.91.1
    expect_compile 3.12.40
    expect_pr 3.12.40 0.91.1 "uv 0.91.0 → 0.91.1"
    expect_isolated
}

patch_and_uv() {
    setup 3.12.40 0.91.0 0.91.1 3.12.41 3.12.40
    expect_success
    expect_compile 3.12.41
    expect_pr 3.12.41 0.91.1 "Python 3.12.40 → 3.12.41, uv 0.91.0 → 0.91.1"
    expect_isolated
}

pin_newer_than_list() {
    setup 3.12.45 0.91.0 0.91.0 3.12.41
    expect_success
    grep -qF "keeping master's newer 3.12.45" "$case_dir/log" || fail "no notice about the newer pin"
    expect_no_pr
}

pin_newer_than_list_with_new_uv() {
    setup 3.12.45 0.91.0 0.91.1 3.12.41
    expect_success
    expect_compile 3.12.45
    expect_pr 3.12.45 0.91.1 "uv 0.91.0 → 0.91.1"
}

follows_master_minor() {
    setup 3.13.20 0.91.0 0.91.0 3.12.60 3.13.21 3.13.19
    expect_success
    expect_compile 3.13.21
    expect_pr 3.13.21 0.91.0 "Python 3.13.20 → 3.13.21"
}

malformed_python_pin() {
    setup 3.12 0.91.0 0.91.0 3.12.41
    expect_failure "Python pin is not one exact X.Y.Z version: '3.12'"
    expect_no_pr
    [[ ! -s "$state/gh-calls" ]] || fail "GitHub was called before the pins were checked"
}

malformed_uv_pin() {
    setup 3.12.40 v0.91.0 0.91.0 3.12.41
    expect_failure "uv pin is not one exact X.Y.Z version: 'v0.91.0'"
    expect_no_pr
}

two_line_python_pin() {
    setup 3.12.40 0.91.0 0.91.0 3.12.41
    printf '3.12.40\n3.12.41\n' > "$checkout/.python-version"
    expect_failure "Python pin is not one exact X.Y.Z version"
}

sb_digest_mismatch() {
    FAKE_SB_DIGEST=$(printf 'other' | sha256sum | cut -c1-64) setup 3.12.40 0.91.0 0.91.1 3.12.41
    expect_failure "sb_linux_amd64: SHA-256 mismatch"
    [[ ! -s "$state/sb-calls" ]] || fail "sb ran despite a digest mismatch"
    [[ ! -s "$state/uv-calls" ]] || fail "uv ran despite a digest mismatch"
    expect_no_pr
}

sb_without_digest() {
    setup 3.12.40 0.91.0 0.91.1 3.12.41
    jq '.assets |= map(del(.digest))' "$state/releases/${sb_repo//\//_}.json" > "$state/r.json"
    mv "$state/r.json" "$state/releases/${sb_repo//\//_}.json"
    expect_failure "has no single sb_linux_amd64 asset with a sha256 digest"
    [[ ! -s "$state/sb-calls" ]] || fail "sb ran without a digest"
}

uv_checksum_mismatch() {
    setup 3.12.40 0.91.0 0.91.1 3.12.41
    printf '%s  uv-x86_64-unknown-linux-gnu.tar.gz\n' "$(printf 'other' | sha256sum | cut -c1-64)" \
        > "$state/assets/astral-sh_uv/0.91.1/uv-x86_64-unknown-linux-gnu.tar.gz.sha256"
    expect_failure "uv 0.91.1 uv-x86_64-unknown-linux-gnu.tar.gz: SHA-256 mismatch"
    [[ ! -s "$state/uv-calls" ]] || fail "uv ran despite a checksum mismatch"
    expect_no_pr
}

sb_without_uv_version() {
    setup 3.12.40 0.91.0 0.91.0 3.12.41
    sb_release '{"version":"9.9.0","git_commit":"abc1234"}'
    expect_failure "sb 9.9.0 does not report uv_version in 'sb version --json'"
    expect_no_pr
}

preflight_failure() {
    setup 3.12.40 0.91.0 0.91.0 3.12.41
    touch "$state/preflight-fail-certbot"
    expect_failure "preflight: certbot --version failed"
    expect_no_pr
}

reproducibility_failure() {
    setup 3.12.40 0.91.0 0.91.0 3.12.41
    touch "$state/repro-drift"
    expect_failure "CI's lock compile does not reproduce the new lock; refusing"
    expect_no_pr
}

existing_pr_updated() {
    setup 3.12.40 0.91.0 0.91.1 3.12.41
    printf '%s\n' 2222222222222222222222222222222222222222 > "$state/branch"
    jq -n '[{number: 41, state: "open", title: "chore(deps): update python toolchain",
             head: "python-toolchain", base: "master", body: "Python 3.12.40 → 3.12.41"},
            {number: 12, state: "closed", head: "python-toolchain", base: "master", body: "old"}]' \
        > "$state/pulls.json"
    expect_success
    grep -qx "update pull request 41" "$state/gh-writes" || fail "PR #41 not updated"
    ! grep -q "open pull request" "$state/gh-writes" || fail "a second PR was opened"
    grep -qx "move branch" "$state/gh-writes" || fail "branch not moved"
    jq -e '[.[] | select(.number == 41)][0].body | startswith("Python 3.12.40 → 3.12.41, uv 0.91.0 → 0.91.1\n")' \
        "$state/pulls.json" > /dev/null || fail "PR #41 body not updated"
    grep -qx "enable auto-merge 41" "$state/gh-writes" || fail "auto-merge not enabled on #41"
    : > "$state/gh-writes"
    expect_success
    ! grep -q "create commit\|move branch\|open pull request" "$state/gh-writes" ||
        fail "rerun rewrote an up-to-date branch or opened a PR: $(tr '\n' ';' < "$state/gh-writes")"
    grep -qx "update pull request 41" "$state/gh-writes" || fail "rerun did not update PR #41"
}

master_moved() {
    setup 3.12.40 0.91.0 0.91.0 3.12.41
    BETWEEN_STEPS="printf '%s\n' 3333333333333333333333333333333333333333 > '$state/master-sha'" \
        expect_failure "master moved from $base to 3333333333333333333333333333333333333333"
    ! grep -q "branch\|pull request" "$state/gh-writes" 2> /dev/null || fail "published onto a moved master"
}

unsigned_commit() {
    setup 3.12.40 0.91.0 0.91.0 3.12.41
    touch "$state/unsigned"
    expect_failure "is not signed by GitHub; not publishing it"
    ! grep -q "branch\|pull request" "$state/gh-writes" || fail "published an unsigned commit"
}

ci_command_matches() {
    local ci expected
    ci=$(sed -n '/^uv pip compile/,/requirements-saltbox\.in$/p' "$repo_root/.github/scripts/check-python-environment.sh" |
        tr -d '\\"' | tr -s ' \n' '  ' | sed 's/ $//')
    # shellcheck disable=SC2016 # The literal variable name CI's script uses.
    expected='uv pip compile --python-version $python_version --generate-hashes --output-file requirements/requirements-saltbox.txt requirements/requirements-saltbox.in'
    [[ $ci == "$expected" ]] || fail "check-python-environment.sh compiles with: $ci"
    # shellcheck disable=SC2016 # Matches update.sh source text, not an expansion.
    grep -qF 'pip compile --python-version "$1" --generate-hashes --output-file "$lock" "$lock_in"' "$update" ||
        fail "update.sh no longer runs CI's compile command"
}

run_case "nothing newer: no PR" nothing_newer
run_case "sb uv older than master's .uv-version: uv never goes down" sb_uv_older_than_pin
run_case "newer patch only" patch_only
run_case "newer uv only" uv_only
run_case "newer patch and uv" patch_and_uv
run_case "pin newer than uv lists: refuses to go down" pin_newer_than_list
run_case "pin newer than uv lists, newer uv: Python stays" pin_newer_than_list_with_new_uv
run_case "master on 3.13: follows 3.13, never changes the minor" follows_master_minor
run_case "malformed .python-version" malformed_python_pin
run_case "malformed .uv-version" malformed_uv_pin
run_case "two-line .python-version" two_line_python_pin
run_case "sb digest mismatch" sb_digest_mismatch
run_case "sb asset without digest" sb_without_digest
run_case "uv checksum mismatch" uv_checksum_mismatch
run_case "sb without uv_version" sb_without_uv_version
run_case "preflight failure: no PR" preflight_failure
run_case "reproducibility failure: no PR" reproducibility_failure
run_case "existing PR: updated, not duplicated" existing_pr_updated
run_case "master moved between steps: refuses" master_moved
run_case "unsigned commit: not published" unsigned_commit
run_case "CI lock compile command matches" ci_command_matches

echo "$passes passed, $failures failed"
((failures == 0))
