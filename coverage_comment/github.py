import dataclasses
import functools
import io
import json
import pathlib
import sys
import zipfile

from coverage_comment import github_client, log

GITHUB_ACTIONS_LOGIN = "github-actions[bot]"
MISSING_COVERAGE_MESSAGE = "This line has no coverage"


class CannotDeterminePR(Exception):
    pass


class CannotPostComment(Exception):
    pass


class NoArtifact(Exception):
    pass


@dataclasses.dataclass
class RepositoryInfo:
    default_branch: str
    visibility: str

    def is_default_branch(self, ref: str) -> bool:
        return f"refs/heads/{self.default_branch}" == ref

    def is_public(self) -> bool:
        return self.visibility == "public"


def get_repository_info(
    github: github_client.GitHub, repository: str
) -> RepositoryInfo:
    response = github.repos(repository).get()

    return RepositoryInfo(
        default_branch=response.default_branch, visibility=response.visibility
    )


def download_artifact(
    github: github_client.GitHub,
    repository: str,
    artifact_name: str,
    run_id: int,
    filename: pathlib.Path,
) -> str:
    repo_path = github.repos(repository)
    artifacts = repo_path.actions.runs(run_id).artifacts.get().artifacts
    try:
        artifact = next(
            iter(artifact for artifact in artifacts if artifact.name == artifact_name),
        )
    except StopIteration:
        raise NoArtifact(
            f"Not artifact found with name {artifact_name} in run {run_id}"
        )

    zip_bytes = io.BytesIO(repo_path.actions.artifacts(artifact.id).zip.get(bytes=True))
    zipf = zipfile.ZipFile(zip_bytes)

    try:
        return zipf.open(str(filename), "r").read().decode("utf-8")
    except KeyError:
        raise NoArtifact(f"File named {filename} not found in artifact {artifact_name}")


def get_pr_number_from_workflow_run(
    github: github_client.GitHub, repository: str, run_id: int
) -> int:
    # It's quite horrendous to access the PR number from a workflow run,
    # especially when it's not the "pull_request" workflow run itself but a
    # "workflow_run" workflow run that runs after the "pull_request" workflow
    # run.
    #
    # 1. We need the user to give us access to the "pull_request" workflow run
    #    id. That's why we request to be sent the following as input:
    #    GITHUB_PR_RUN_ID: ${{ github.event.workflow_run.id }}
    # 2. From that run, we get the corresponding branch, and the owner of the branch
    # 3. We list open PRs that have that branch as head branch. There should be only
    #    one.
    # 4. If there's no open PRs, we look at all PRs. We take the most recently
    #    updated one

    repo_path = github.repos(repository)
    run = repo_path.actions.runs(run_id).get()
    branch = run.head_branch
    repo_name = run.head_repository.full_name
    full_branch = f"{repo_name}:{branch}"
    get_prs = functools.partial(
        repo_path.pulls.get,
        head=full_branch,
        sort="updated",
        direction="desc",
    )
    try:
        return next(iter(pr.number for pr in get_prs(state="open")))
    except StopIteration:
        pass
    log.info(f"No open PR found for branch {full_branch}, defaulting to all PRs")

    try:
        return next(iter(pr.number for pr in get_prs(state="all")))
    except StopIteration:
        raise CannotDeterminePR(f"No open PR found for branch {full_branch}")


def get_my_login(github: github_client.GitHub) -> str:
    try:
        response = github.user.get()
    except github_client.Forbidden:
        # The GitHub actions user cannot access its own details
        # and I'm not sure there's a way to see that we're using
        # the GitHub actions user except noting that it fails
        return GITHUB_ACTIONS_LOGIN

    else:
        return response.login


def post_comment(
    github: github_client.GitHub,
    me: str,
    repository: str,
    pr_number: int,
    contents: str,
    marker: str,
) -> None:
    issue_comments_path = github.repos(repository).issues(pr_number).comments
    comments_path = github.repos(repository).issues.comments

    for comment in issue_comments_path.get():
        if comment.user.login == me and marker in comment.body:
            log.info("Update previous comment")
            try:
                comments_path(comment.id).patch(body=contents)
            except github_client.Forbidden as exc:
                raise CannotPostComment from exc
            break
    else:
        log.info("Adding new comment")
        try:
            issue_comments_path.post(body=contents)
        except github_client.Forbidden as exc:
            raise CannotPostComment from exc


def set_output(github_output: pathlib.Path | None, **kwargs: bool) -> None:
    if github_output:
        with github_output.open("a") as f:
            for key, value in kwargs.items():
                f.write(f"{key}={json.dumps(value)}\n")


def escape_property(s: str) -> str:
    return (
        s.replace("%", "%25")
        .replace("\r", "%0D")
        .replace("\n", "%0A")
        .replace(":", "%3A")
        .replace(",", "%2C")
    )


def escape_data(s: str) -> str:
    return s.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def get_workflow_command(command: str, command_value: str, **kwargs: str) -> str:
    """
    Returns a string that can be printed to send a workflow command
    https://docs.github.com/en/actions/using-workflows/workflow-commands-for-github-actions
    """
    values_listed = [f"{key}={escape_property(value)}" for key, value in kwargs.items()]

    context = f" {','.join(values_listed)}" if values_listed else ""
    return f"::{command}{context}::{escape_data(command_value)}"


def send_workflow_command(command: str, command_value: str, **kwargs: str) -> None:
    print(
        get_workflow_command(command=command, command_value=command_value, **kwargs),
        file=sys.stderr,
    )


def create_missing_coverage_annotation(
    annotation_type: str, file: pathlib.Path, line: int
):
    send_workflow_command(
        command=annotation_type,
        command_value=MISSING_COVERAGE_MESSAGE,
        # This will produce \ paths when running on windows.
        # GHA doc is unclear whether this is right or not.
        file=str(file),
        line=str(line),
    )


def append_to_file(content: str, filepath: pathlib.Path):
    with filepath.open(mode="a") as file:
        file.write(content)


def add_job_summary(content: str, github_step_summary: pathlib.Path):
    append_to_file(content=content, filepath=github_step_summary)
