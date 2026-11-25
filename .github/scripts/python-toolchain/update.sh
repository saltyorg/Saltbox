#!/usr/bin/env bash
# Keep master's Python patch, uv minimum and hashed lock current.
#
#   update.sh prepare <result-dir>   read-only: find targets, compile, preflight
#   update.sh publish <result-dir>   write: commit, branch, PR and auto-merge
#   update.sh required-checks        checks master must require for auto-merge
#
# publish enables auto-merge only when master requires every one of
# required-checks; otherwise it leaves the PR open, says why in its body,
# and fails.
#
# prepare environment:
#   SB_RELEASE_REPO   sb repository whose latest stable release owns uv
#   SALTBOX_DIR       master checkout (default: current directory)
#   GH_TOKEN          read token for the GitHub API
# publish environment:
#   GITHUB_REPOSITORY repository to open the PR on
#   GH_TOKEN          App token with contents and pull-requests write

set -euo pipefail

script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source-path=SCRIPTDIR
source "$script_dir/lib.sh"

sb_asset=sb_linux_amd64
uv_repo=astral-sh/uv
uv_asset='uv-x86_64-unknown-linux-gnu.tar.gz'
pr_branch=python-toolchain
base_branch=master
commit_title='chore(deps): update python toolchain'
age_floor_seconds=$((7 * 24 * 60 * 60))
lock=requirements/requirements-saltbox.txt
lock_in=requirements/requirements-saltbox.in
# Checks master must require before the PR may auto-merge: every OS of
# saltbox.yml's Python environment job, and this script's tests.
required_checks=(
    "Python environment-22.04"
    "Python environment-24.04"
    "Python environment-26.04"
    python-toolchain-tests
)
saltbox_dir=${SALTBOX_DIR:-$PWD}
temp_root=${RUNNER_TEMP:-${TMPDIR:-/tmp}}

# The lock compile Saltbox CI runs in check-python-environment.sh.
ci_compile_args() {
    printf '%s\n' pip compile --python-version "$1" --generate-hashes --output-file "$lock" "$lock_in"
}

ci_compile_command() {
    local -a args
    mapfile -t args < <(ci_compile_args "$1")
    printf 'uv %s\n' "${args[*]}"
}

latest_sb_uv() {
    local work=$1 release asset_digest tag sb_dir sb_bin json uv
    release="$work/sb-release.json"
    gh api "repos/$SB_RELEASE_REPO/releases/latest" > "$release" ||
        toolchain_fail "cannot read the latest release of $SB_RELEASE_REPO"
    jq -e '.draft == false and .prerelease == false' "$release" > /dev/null ||
        toolchain_fail "the latest release of $SB_RELEASE_REPO is a draft or prerelease"
    tag=$(jq -er '.tag_name' "$release") || toolchain_fail "the latest $SB_RELEASE_REPO release has no tag"
    asset_digest=$(jq -r --arg name "$sb_asset" '[.assets[] | select(.name == $name)] | if length == 1 then .[0].digest // "" else "" end' "$release")
    [[ $asset_digest =~ ^sha256:[0-9a-f]{64}$ ]] ||
        toolchain_fail "$SB_RELEASE_REPO $tag has no single $sb_asset asset with a sha256 digest"
    sb_dir="$work/sb"
    mkdir -p "$sb_dir"
    gh release download "$tag" --repo "$SB_RELEASE_REPO" --pattern "$sb_asset" --dir "$sb_dir" >&2 ||
        toolchain_fail "cannot download $sb_asset from $SB_RELEASE_REPO $tag"
    sb_bin="$sb_dir/$sb_asset"
    verify_sha256 "$sb_bin" "${asset_digest#sha256:}" "$SB_RELEASE_REPO $tag $sb_asset"
    chmod 0755 "$sb_bin"
    mv -- "$sb_bin" "$sb_dir/sb"
    json=$(cd -- "$sb_dir" && env -i PATH=/usr/bin:/bin HOME="$sb_dir" ./sb version --json) ||
        toolchain_fail "sb $tag failed to run 'sb version --json'"
    uv=$(jq -r '.uv_version // empty | strings' <<< "$json" 2> /dev/null) || uv=""
    [[ -n $uv ]] || toolchain_fail "sb $tag does not report uv_version in 'sb version --json'"
    is_exact_version "$uv" || toolchain_fail "sb $tag reports an invalid uv_version: '$uv'"
    printf '%s %s\n' "$tag" "$uv"
}

download_uv() {
    local work=$1 version=$2 dir expected named uv_bin
    dir="$work/uv-download"
    mkdir -p "$dir"
    gh release download "$version" --repo "$uv_repo" \
        --pattern "$uv_asset" --pattern "$uv_asset.sha256" --dir "$dir" >&2 ||
        toolchain_fail "cannot download uv $version from $uv_repo"
    [[ -f "$dir/$uv_asset" && -f "$dir/$uv_asset.sha256" ]] ||
        toolchain_fail "uv $version release lacks $uv_asset or its .sha256"
    read -r expected named < "$dir/$uv_asset.sha256" || true
    [[ ${named#\*} == "$uv_asset" ]] || toolchain_fail "uv $version .sha256 does not name $uv_asset"
    verify_sha256 "$dir/$uv_asset" "$expected" "uv $version $uv_asset"
    mkdir -p "$work/uv"
    tar --extract --gzip --file "$dir/$uv_asset" --directory "$work/uv" --no-same-owner
    uv_bin=$(find "$work/uv" -type f -name uv -print -quit)
    [[ -n $uv_bin ]] || toolchain_fail "uv $version archive holds no uv binary"
    chmod 0755 "$uv_bin"
    [[ $(isolated "$work" uv "$work" "$uv_bin" --version) =~ ^uv\ ${version//./\\.}( |$) ]] ||
        toolchain_fail "downloaded uv does not report version $version"
    printf '%s\n' "$uv_bin"
}

newest_patch() {
    local work=$1 uv_bin=$2 minor=$3 catalog
    catalog="$work/python-downloads.json"
    isolated "$work" uv "$work" "$uv_bin" python list "cpython@$minor" \
        --only-downloads --output-format json > "$catalog" ||
        toolchain_fail "uv python list failed"
    jq -r --arg minor "$minor" '
        [
          .[]
          | select(.implementation == "cpython" and .os == "linux" and .arch == "x86_64"
                   and .libc == "gnu" and .variant == "default")
          | select("\(.version_parts.major).\(.version_parts.minor)" == $minor)
          | select(.version | test("^[0-9]+\\.[0-9]+\\.[0-9]+$"))
        ]
        | sort_by(.version_parts.major, .version_parts.minor, .version_parts.patch)
        | last
        | .version // empty
    ' "$catalog"
}

# Names of every package master's lock pins, for the age-floor exemption.
locked_packages() {
    sed -n 's/^\([A-Za-z0-9][A-Za-z0-9._-]*\)\(\[[^]]*\]\)\{0,1\}==.*/\1/p' "$1" | sort -u
}

prepare() {
    local result=$1 saltbox=$saltbox_dir work
    : "${SB_RELEASE_REPO:?SB_RELEASE_REPO is required}"
    saltbox=$(cd -- "$saltbox" && pwd)
    mkdir -p "$result"
    result=$(cd -- "$result" && pwd)
    work=$(mktemp -d "$temp_root/python-toolchain.XXXXXXXXXX")
    # shellcheck disable=SC2064 # Expand now: work is local to this function.
    trap "rm -rf -- $(printf '%q' "$work")" EXIT

    local base current_python current_uv minor
    base=$(git -C "$saltbox" rev-parse HEAD)
    current_python=$(read_exact_version "$saltbox/.python-version" Python)
    current_uv=$(read_exact_version "$saltbox/.uv-version" uv)
    minor=${current_python%.*}
    toolchain_log "master pins Python $current_python (minor $minor) and uv $current_uv"

    local sb_info sb_tag sb_uv target_uv
    sb_info=$(latest_sb_uv "$work")
    read -r sb_tag sb_uv <<< "$sb_info"
    target_uv=$(version_max "$sb_uv" "$current_uv")
    toolchain_log "$SB_RELEASE_REPO $sb_tag carries uv $sb_uv; target uv $target_uv"

    local uv_bin candidate target_python
    uv_bin=$(download_uv "$work" "$target_uv")
    candidate=$(newest_patch "$work" "$uv_bin" "$minor")
    [[ -n $candidate ]] || toolchain_fail "uv $target_uv lists no CPython $minor Linux x86_64 GNU build"
    target_python=$(version_max "$candidate" "$current_python")
    if version_lt "$candidate" "$current_python"; then
        toolchain_log "uv $target_uv lists Python $candidate at most; keeping master's newer $current_python"
    fi
    toolchain_log "target Python $target_python"

    printf 'base=%s\nfrom_python=%s\nto_python=%s\nfrom_uv=%s\nto_uv=%s\nsb_release=%s %s\n' \
        "$base" "$current_python" "$target_python" "$current_uv" "$target_uv" "$SB_RELEASE_REPO" "$sb_tag" \
        > "$result/metadata"

    if [[ $target_python == "$current_python" && $target_uv == "$current_uv" ]]; then
        toolchain_log "master is current; nothing to do"
        printf 'changed=false\n' >> "$result/metadata"
        write_output changed false
        return 0
    fi

    local candidate_dir="$work/candidate" floor
    mkdir -p "$candidate_dir"
    cp -a -- "$saltbox/requirements" "$candidate_dir/requirements"
    isolated "$work" uv "$work" "$uv_bin" python install --managed-python --no-bin "$target_python"

    floor=$(date -u -d "@$((${TOOLCHAIN_NOW:-$(date +%s)} - age_floor_seconds))" +%Y-%m-%dT%H:%M:%SZ)
    local -a exempt=() compile
    local package
    while IFS= read -r package; do
        exempt+=(--exclude-newer-package "$package=false")
    done < <(locked_packages "$saltbox/$lock")
    mapfile -t compile < <(ci_compile_args "$target_python")
    toolchain_log "compiling the lock with uv $target_uv, new packages limited to uploads before $floor"
    isolated "$work" uv "$candidate_dir" "$uv_bin" "${compile[@]:0:2}" \
        --exclude-newer "$floor" "${exempt[@]}" \
        --custom-compile-command "$(ci_compile_command "$target_python")" \
        "${compile[@]:2}" > /dev/null || toolchain_fail "lock compile failed"

    preflight "$work" "$uv_bin" "$target_python" "$candidate_dir/$lock"

    local repro_dir="$work/reproduce"
    cp -a -- "$candidate_dir" "$repro_dir"
    isolated "$work" uv "$repro_dir" "$uv_bin" "${compile[@]}" > /dev/null ||
        toolchain_fail "CI's lock compile failed on the new lock"
    if ! cmp -s -- "$candidate_dir/$lock" "$repro_dir/$lock"; then
        diff -u -- "$candidate_dir/$lock" "$repro_dir/$lock" >&2 || true
        toolchain_fail "CI's lock compile does not reproduce the new lock; refusing"
    fi
    toolchain_log "CI's lock compile reproduces the new lock"

    mkdir -p "$result/files/requirements"
    printf '%s\n' "$target_python" > "$result/files/.python-version"
    printf '%s\n' "$target_uv" > "$result/files/.uv-version"
    cp -- "$candidate_dir/$lock" "$result/files/$lock"
    printf 'changed=true\n' >> "$result/metadata"
    write_output changed true
}

preflight() {
    local work=$1 uv_bin=$2 version=$3 new_lock=$4 python_bin venv tool
    venv="$work/preflight-venv"
    python_bin=$(isolated "$work" uv "$work" "$uv_bin" python find \
        --managed-python --no-project --no-python-downloads "$version") ||
        toolchain_fail "preflight: Python $version is not installed"
    [[ $(isolated "$work" python "$work" "$python_bin" --version) == "Python $version" ]] ||
        toolchain_fail "preflight: installed Python does not report $version"
    isolated "$work" uv "$work" "$uv_bin" venv --python "$python_bin" \
        --no-project --no-python-downloads "$venv" ||
        toolchain_fail "preflight: cannot create the scratch venv"
    isolated "$work" uv "$work" "$uv_bin" pip sync --python "$venv/bin/python" \
        --require-hashes "$new_lock" || toolchain_fail "preflight: hashed install failed"
    isolated "$work" uv "$work" "$uv_bin" pip check --python "$venv/bin/python" ||
        toolchain_fail "preflight: uv pip check failed"
    for tool in ansible certbot apprise; do
        isolated "$work" entrypoint "$venv" "$venv/bin/$tool" --version ||
            toolchain_fail "preflight: $tool --version failed"
    done
    toolchain_log "preflight passed"
}

write_output() {
    if [[ -n ${GITHUB_OUTPUT:-} ]]; then
        printf '%s=%s\n' "$1" "$2" >> "$GITHUB_OUTPUT"
    fi
}

meta() {
    sed -n "s/^$1=//p" "$2/metadata"
}

change_summary() {
    local result=$1 from_python to_python from_uv to_uv summary=""
    from_python=$(meta from_python "$result")
    to_python=$(meta to_python "$result")
    from_uv=$(meta from_uv "$result")
    to_uv=$(meta to_uv "$result")
    [[ $from_python == "$to_python" ]] || summary="Python $from_python → $to_python"
    if [[ $from_uv != "$to_uv" ]]; then
        summary+="${summary:+, }uv $from_uv → $to_uv"
    fi
    printf '%s\n' "$summary"
}

publish() {
    local result=$1 repo=${GITHUB_REPOSITORY:?GITHUB_REPOSITORY is required}
    [[ $(meta changed "$result") == true ]] || toolchain_fail "no prepared change to publish"
    local base summary body master_head base_tree tree commit_json commit
    base=$(meta base "$result")
    summary=$(change_summary "$result")
    body="$summary

uv follows the latest stable sb release ($(meta sb_release "$result")). The lock was compiled from master's lock, with packages new to it limited to uploads at least 7 days old, then preflighted and reproduced by CI's lock compile."

    master_head=$(gh api "repos/$repo/git/ref/heads/$base_branch" --jq .object.sha) ||
        toolchain_fail "cannot read $base_branch"
    [[ $master_head == "$base" ]] ||
        toolchain_fail "$base_branch moved from $base to $master_head since prepare; rerun"

    local merge_block="" number
    merge_block=$(auto_merge_blocker "$repo")
    number=$(gh api "repos/$repo/pulls?state=open&base=$base_branch&head=${repo%%/*}:$pr_branch" --jq '.[].number') ||
        toolchain_fail "cannot list pull requests"
    [[ $number != *[!0-9]* ]] || toolchain_fail "more than one open $pr_branch pull request: $number"
    if [[ -n $merge_block ]]; then
        toolchain_log "auto-merge refused: $merge_block"
        body+="

Auto-merge is off: $merge_block. Merge by hand once CI passes."
        if [[ -n $number && $(gh api "repos/$repo/pulls/$number" --jq '.auto_merge != null') != false ]]; then
            gh pr merge "$number" --repo "$repo" --disable-auto ||
                toolchain_fail "cannot disable auto-merge on pull request #$number; not updating it"
            toolchain_log "disabled auto-merge on pull request #$number"
        fi
    fi

    base_tree=$(gh api "repos/$repo/git/commits/$base" --jq .tree.sha) ||
        toolchain_fail "cannot read the base commit"
    tree=$(jq -n --arg base_tree "$base_tree" \
        --rawfile python "$result/files/.python-version" \
        --rawfile uv "$result/files/.uv-version" \
        --rawfile lock "$result/files/$lock" --arg lock_path "$lock" '
        {base_tree: $base_tree, tree: [
          {path: ".python-version", mode: "100644", type: "blob", content: $python},
          {path: ".uv-version", mode: "100644", type: "blob", content: $uv},
          {path: $lock_path, mode: "100644", type: "blob", content: $lock}
        ]}' | gh api -X POST "repos/$repo/git/trees" --input - --jq .sha) ||
        toolchain_fail "cannot create the tree"

    commit=$(reuse_branch_commit "$repo" "$base" "$tree")
    if [[ -z $commit ]]; then
        commit_json=$(jq -n --arg message "$commit_title"$'\n\n'"$summary" \
            --arg tree "$tree" --arg parent "$base" \
            '{message: $message, tree: $tree, parents: [$parent]}' |
            gh api -X POST "repos/$repo/git/commits" --input -) ||
            toolchain_fail "cannot create the commit"
        commit=$(jq -er '.sha' <<< "$commit_json") || toolchain_fail "commit response has no sha"
        jq -e '.verification.verified == true' <<< "$commit_json" > /dev/null ||
            toolchain_fail "commit $commit is not signed by GitHub; not publishing it"
        set_branch "$repo" "$commit"
    fi
    toolchain_log "branch $pr_branch is at $commit"

    if [[ -n $number ]]; then
        jq -n --arg title "$commit_title" --arg body "$body" '{title: $title, body: $body}' |
            gh api -X PATCH "repos/$repo/pulls/$number" --input - > /dev/null ||
            toolchain_fail "cannot update pull request #$number"
        toolchain_log "updated pull request #$number"
    else
        number=$(jq -n --arg title "$commit_title" --arg body "$body" \
            --arg head "$pr_branch" --arg base "$base_branch" \
            '{title: $title, body: $body, head: $head, base: $base}' |
            gh api -X POST "repos/$repo/pulls" --input - --jq .number) ||
            toolchain_fail "cannot open the pull request"
        toolchain_log "opened pull request #$number"
    fi
    [[ -z $merge_block ]] ||
        toolchain_fail "pull request #$number left open without auto-merge: $merge_block"
    gh pr merge "$number" --repo "$repo" --auto --squash ||
        toolchain_fail "cannot enable auto-merge on pull request #$number"
    toolchain_log "auto-merge enabled on pull request #$number: $summary"
}

# Print why auto-merge must not be enabled, or nothing when master requires
# every check in required_checks. Required checks come from branch
# protection (the branch endpoint, readable with contents: read; the
# protection endpoint needs administration: read, which the App token is
# not given) and from active repository and organisation rulesets. A
# failed read blocks auto-merge.
auto_merge_blocker() {
    local repo=$1 branch_json rules_json required check missing=()
    if ! branch_json=$(gh api "repos/$repo/branches/$base_branch" 2> /dev/null) ||
        ! rules_json=$(gh api --paginate "repos/$repo/rules/branches/$base_branch" 2> /dev/null) ||
        ! required=$(
            jq -r 'select(.protection.enabled == true and .protection.required_status_checks.enforcement_level != "off")
                | .protection.required_status_checks | (.contexts[]?, .checks[]?.context)' <<< "$branch_json" &&
                jq -r '.[] | select(.type == "required_status_checks") | .parameters.required_status_checks[].context' <<< "$rules_json"
        ); then
        printf "cannot read %s's required status checks\n" "$base_branch"
        return 0
    fi
    for check in "${required_checks[@]}"; do
        grep -qxF -- "$check" <<< "$required" || missing+=("$check")
    done
    if ((${#missing[@]})); then
        local joined=""
        for check in "${missing[@]}"; do
            joined+="${joined:+, }$check"
        done
        printf '%s does not require the status checks %s\n' "$base_branch" "$joined"
    fi
}

# Print the branch's head when it already is exactly this change on base.
reuse_branch_commit() {
    local repo=$1 base=$2 tree=$3 head
    head=$(gh api "repos/$repo/git/ref/heads/$pr_branch" --jq .object.sha 2> /dev/null) || return 0
    gh api "repos/$repo/git/commits/$head" 2> /dev/null |
        jq -e --arg base "$base" --arg tree "$tree" \
            '.tree.sha == $tree and (.parents | map(.sha)) == [$base] and .verification.verified == true' \
            > /dev/null 2>&1 || return 0
    printf '%s\n' "$head"
}

set_branch() {
    local repo=$1 commit=$2
    if gh api "repos/$repo/git/ref/heads/$pr_branch" > /dev/null 2>&1; then
        gh api -X PATCH "repos/$repo/git/refs/heads/$pr_branch" -f sha="$commit" -F force=true > /dev/null ||
            toolchain_fail "cannot move $pr_branch"
    else
        gh api -X POST "repos/$repo/git/refs" -f ref="refs/heads/$pr_branch" -f sha="$commit" > /dev/null ||
            toolchain_fail "cannot create $pr_branch"
    fi
}

case "${1:-}" in
    prepare) prepare "${2:?result directory required}" ;;
    publish) publish "${2:?result directory required}" ;;
    required-checks) printf '%s\n' "${required_checks[@]}" ;;
    *) echo "usage: $0 prepare|publish <result-dir> | required-checks" >&2; exit 2 ;;
esac
