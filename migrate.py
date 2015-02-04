#!/usr/bin/env python
#-*- coding: utf-8 -*-

# This file is part of the bitbucket issue migration script.
#
# The script is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# The script is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with the bitbucket issue migration script.
# If not, see <http://www.gnu.org/licenses/>.

from __future__ import print_function, unicode_literals

import argparse
import getpass
import operator
import itertools

from pygithub3 import Github

try:
    import keyring
except ImportError:
    # keyring isn't available, so mock the interface to simulate no pw
    class Keyring:
        get_password = staticmethod(lambda system, username: None)

try:
    import json
except ImportError:
    import simplejson as json

from six import text_type
from six.moves import urllib
from jaraco.functools import compose
from jaraco.itertools import Counter


def read_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "A tool to migrate issues from Bitbucket to GitHub.\n"
            "note: the Bitbucket repository and issue tracker have to be"
            "public"
        )
    )

    parser.add_argument(
        "bitbucket_username",
        help="Bitbucket username of the source repository."
    )

    parser.add_argument(
        "bitbucket_repo",
        help="Bitbucket project name for the source repo."
    )

    parser.add_argument(
        "github_username",
        help="Your GitHub username"
    )

    parser.add_argument(
        "github_repo",
        help="GitHub to add issues to. Format: <username>/<repo name>"
    )

    parser.add_argument(
        "-n", "--dry-run",
        action="store_true", dest="dry_run", default=False,
        help="Perform a dry run and print eveything."
    )

    parser.add_argument(
        "-f", "--start_id", type=int, dest="start", default=0,
        help="Bitbucket issue id from which to start import"
    )

    return parser.parse_args()


# Formatters
def format_user(author_info):
    if not author_info:
        return "Anonymous"

    if author_info['first_name'] and author_info['last_name']:
        return " ".join([author_info['first_name'], author_info['last_name']])

    if 'username' in author_info:
        return '[{0}](http://bitbucket.org/{0})'.format(
            author_info['username']
        )


def format_name(issue):
    if 'reported_by' in issue:
        return format_user(issue['reported_by'])
    else:
        return "Anonymous"


def format_body(options, issue):
    content = clean_body(issue.get('content'))
    return """{}

{}
- Bitbucket: https://bitbucket.org/{}/{}/issue/{}
- Originally reported by: {}
- Originally created at: {}
""".format(
        content,
        '-' * 40,
        options.bitbucket_username, options.bitbucket_repo, issue['local_id'],
        format_name(issue),
        issue['created_on']
    )


def format_comment(comment):
    return """{}

{}
Original comment by: {}
""".format(
        comment['body'],
        '-' * 40,
        comment['user'],
    )


def clean_body(body):
    lines = []
    in_block = False
    for line in text_type(body).splitlines():
        if line.startswith("{{{") or line.startswith("}}}"):
            if "{{{" in line:
                before, part, after = line.partition("{{{")
                lines.append('    ' + after)
                in_block = True

            if "}}}" in line:
                before, part, after = line.partition("}}}")
                lines.append('    ' + before)
                in_block = False
        else:
            if in_block:
                lines.append("    " + line)
            else:
                lines.append(line.replace("{{{", "`").replace("}}}", "`"))
    return "\n".join(lines)


def _iter_issues(bb_url, start_id):
    '''
    Fetch the issues from Bitbucket, one page at a time.
    '''
    url = "{bb_url}/?start={start_id}".format(**locals())

    try:
        response = urllib.request.urlopen(url)
    except urllib.error.HTTPError as ex:
        ex.message = (
            'Problem trying to connect to bitbucket ({url}): {ex} '
            'Hint: the bitbucket repository name is case-sensitive.'
            .format(url=url, ex=ex)
        )
        raise

    result = json.loads(response.read())
    if not result['issues']:
        # No issues encountered at or above start_id
        raise StopIteration()

    next_start = start_id + len(result['issues'])
    return itertools.chain(result['issues'], _iter_issues(bb_url, next_start))


get_issues = compose(list, _iter_issues)


def get_comments(bb_url, issue):
    '''
    Fetch the comments for a Bitbucket issue
    '''
    url = "{bb_url}/{issue[local_id]}/comments/".format(**locals())
    result = json.loads(urllib.request.urlopen(url).read())
    by_creation_date = operator.itemgetter("utc_created_on")
    ordered = sorted(result, key=by_creation_date)
    # filter only those that have content; status comments (assigned,
    # version, etc.) have no body
    filtered = filter(operator.itemgetter('content'), ordered)
    return list(map(_parse_comment, filtered))


def _parse_comment(comment):
    """
    Parse a comment as returned from Bitbucket API.
    """
    return dict(
        user=format_user(comment['author_info']),
        created_at=comment['utc_created_on'],
        body=comment['content'].encode('utf-8'),
        number=comment['comment_id'],
    )


# GitHub push
def push_issue(github, repo_path, issue, body, comments):
    # Create the issue
    gh_username, sep, gh_repository = repo_path.partition('/')
    issue_data = {
        'title': issue['title'],
        'body': body
    }
    new_issue = github.issues.create(
        issue_data,
        gh_username,
        gh_repository
    )

    # Set the status and labels
    if issue.get('status') == 'resolved':
        github.issues.update(
            new_issue.number,
            {'state': 'closed'},
            user=gh_username,
            repo=gh_repository
        )

    # Everything else is done with labels in github
    # TODO: there seems to be a problem with the add_to_issue method of
    #       pygithub3, so it's not possible to assign labels to issues
    elif issue.get('status') == 'wontfix':
        pass
    elif issue.get('status') == 'on hold':
        pass
    elif issue.get('status') == 'invalid':
        pass
    elif issue.get('status') == 'duplicate':
        pass
    elif issue.get('status') == 'wontfix':
        pass

    # github.issues.labels.add_to_issue(
    #     new_issue.number,
    #     issue['metadata']['kind'],
    #     user=gh_username,
    #     repo=gh_repository
    # )

    # github.issues.labels.add_to_issue(
    #     new_issue.number,
    #     gh_username,
    #     gh_repository,
    #     ('import',)
    # )

    # Milestones

    # Add the comments
    for comment in comments:
        github.issues.comments.create(
            new_issue.number,
            format_comment(comment),
            gh_username,
            gh_repository
        )

    print("Created: {} [{} comments]".format(
        issue['title'], len(comments)
    ))


def run():
    options = read_arguments()

    handler_cls = SubmitHandler if not options.dry_run else DryRunHandler
    handler = handler_cls(bb_url)
    handler.run()


class Handler(object):
    bb_base = "https://api.bitbucket.org/1.0/repositories/"
    bb_tmpl = bb_base + "{bitbucket_username}/{bitbucket_repo}/issues"

    def __init__(self, options):
        self.options = options
        self.bb_url = self.bb_tmpl.format(vars(options))

    def get_issues(self):
        issues = get_issues(self.bb_url, self.options.start)
        # In order to sync issue numbers on a freshly-created Github project,
        # sort the issues by local_id
        # Note: not memory efficient and could use too much memory on large
        # projects.
        by_local_id = operator.itemgetter('local_id')
        return sorted(issues, key=by_local_id)

    def run(self):
        self.issues = Counter(self.get_issues)
        for issue in self.issues:
            self.handle(issue)

    def get_comments(self, issue):
        return get_comments(self.bb_url, issue)


class SubmitHandler(Handler):
    def run(self):
        # push them in GitHub (issues comments are fetched here)
        github_password = (
            keyring.get_password('Github', self.options.github_username) or
            getpass.getpass("Please enter your GitHub password\n")
        )
        self.github = Github(
            login=self.options.github_username,
            password=github_password,
        )
        return super(SubmitHandler, self).run()

    def handle(self, issue):
        comments = self.get_comments(issue)
        body = format_body(self.options, issue).encode('utf-8')
        push_issue(self.github, self.options.github_repo, issue, body,
            comments)
        print("Created", self.issues.count, "issues")


class DryRunHandler(Handler):
    def handle(self, issue):
        comments = self.get_comments(issue)
        body = format_body(self.options, issue).encode('utf-8')
        print("Title: {}".format(issue.get('title').encode('utf-8')))
        print("Body: {}".format(body))
        list(map(format_comment, comments))
        print("Comments", [comment['body'] for comment in comments])


if __name__ == "__main__":
    run()
