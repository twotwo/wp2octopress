"""
This script extracts posts from a WordPress database and spits out Octopress-
compatible markdown files.
"""

import codecs
import os
import sys
from collections import defaultdict
from typing import Dict, Tuple

import html2text
from sqlalchemy import create_engine, text

USAGE = "USAGE: {0} db host username password posts_dir pages_dir"
WP_COMMENTS = {"closed": "false", "open": "true"}
WP_PUBLISH = {"draft": "false", "auto-draft": "false", "publish": "true"}

missing_name_count = 0


def fix_post_content(post_content):
    """
    This function does modifies post content to work better with Octopress
    so that you'll hopefully not have to do as much manual editing. Put
    whatever regexes and whatnot you want in here.
    """

    post_content = (post_content).replace("\r\n", "\n")

    # # Replace syntax highlighter blocks with Octopress equivalent
    # post_content = re.sub(
    #     '\[sourcecode language="([A-Za-z0-9]+)"\]', "``` \\1", post_content
    # )
    # post_content = re.sub("\[/sourcecode\]", "", post_content)

    return post_content


def missing_name_check(post):
    """
    Returns a valid name in case of pages and posts that are missing a
    name (which can happen with drafts and such).
    """

    # Globals are bad but I'm lazy and this is an ETL
    global missing_name_count

    if post.post_name.lstrip().rstrip() == "" or "%" in post.post_name:
        name = "".join(
            [
                char
                for char in post.post_title.replace(" ", "-")
                if char.isalnum() or char == "-"
            ]
        )
        if name.lstrip().rstrip() == "":
            name = "missing-name-" + str(missing_name_count)
            missing_name_count += 1
        sys.stderr.write(
            f"Warning: page/post {post.post_title} (ID {post.id}) has bad name. Using name {name}\n"
        )
    else:
        name = post.post_name

    return name


def dump_single_page(page, output_dir):
    """
    Dumps a single page.  Doesn't handle parent-child relationships between
    pages, sorry. You can just mv all the children into a subdirectory or
    whatever you want to do.
    """

    page_name = missing_name_check(page)

    subpath = os.path.join(output_dir, page_name)
    os.makedirs(subpath, exist_ok=True)
    path = os.path.join(subpath, "index.md")
    output = codecs.open(path, encoding="utf-8", mode="w")

    output_params = (
        page.post_title,
        str(page.post_date)[:-3],
        WP_COMMENTS[page.comment_status],
        fix_post_content(page.post_content),
    )

    output.write(
        """---
layout: page
title: "{0}"
date: {1}
comments: {2}
sharing: true
footer: true
---
{3}
""".format(
            *output_params
        )
    )
    output.close()


def refine_file_name(post):
    """
    Returns a valid filename for posts
    """

    name = "".join(
        [
            ch
            for ch in post.post_title.replace(" ", "-")
            if ch.isalnum() or ch == "-"
        ]
    )

    return f"{post.id}-{name}.md"


def dump_single_post(post, post_categories, post_tags, output_dir):
    """
    Dumps a single post (as opposed to a single page)
    """

    post_name = missing_name_check(post)

    filename = f"{post.post_date.year}-{str(post.post_date.month).zfill(2)}-{str(post.post_date.day).zfill(2)}-{post_name}.md"
    filename = refine_file_name(post)
    output = codecs.open(os.path.join(output_dir, filename), encoding="utf-8", mode="w")

    output_params = (
        # "layout: posts",
        f"wp_post_id: {post.id}",
        f'title: "{post.post_title}"',
        f"slug: {post_name}",
        f"date: {str(post.post_date)}",
        f"lastmod: {str(post.post_modified)}",
        f'author: "{post.author}"',
        f"tags: [{', '.join(post_tags.get(post.id) or [])}]",
        f"categories: [{', '.join(post_categories.get(post.id) or [])}]",
        f"comments: {WP_COMMENTS[post.comment_status]}",
        f"published: {WP_PUBLISH[post.post_status]}",
    )

    properties = "\n".join(output_params)
    output.write(f"---\n{properties}\n---\n{fix_post_content(post.post_content)}")
    output.close()


SQL_GET_TAXONOMY = text(
    """SELECT
	object_id AS id, name, taxonomy as type
FROM wp_term_taxonomy
	INNER JOIN wp_terms USING(term_id)
	INNER JOIN wp_term_relationships USING(term_taxonomy_id)
	INNER JOIN wp_posts ON wp_posts.id = object_id
WHERE
    taxonomy IN ('category', 'post_tag') and
    post_type='post' AND
    post_status!='inherit'
ORDER BY id, type"""
)


def _get_taxonomy(db) -> Tuple[Dict, Dict]:
    """
    Get Category and Tag from Table wp_term_taxonomy

    Returns
    -------
    post_categories : Dict
        1 Post -> 0..n Cates, such as {p1: [c1, c2, ..], p2: [c1]}
    post_tags : Dict
        1 Post -> 0..n Tags, such as {p1: [t1, t2, ...], p2: [t3]}

    See Also
    --------
    SQL_GET_TAXONOMY
        select post id, taxonomy name and type

    """
    # https://docs.python.org/3/library/collections.html#collections.defaultdict
    post_categories, post_tags = defaultdict(list), defaultdict(list)
    with db.engine.connect() as conn:
        category_result = conn.execute(SQL_GET_TAXONOMY)

        for row in category_result:
            if "category" == row.type:
                post_categories[row.id].append(row.name)
            else:
                assert "post_tag" == row.type
                post_tags[row.id].append(row.name)

        return post_categories, post_tags


SQL_GET_POST = text(
    """SELECT
    posts.ID               AS `id`,
    posts.post_title              ,
    posts.post_type               ,
    posts.post_status             ,
    posts.post_name               ,
    posts.post_date               ,
    posts.post_modified           ,
    posts.post_content            ,
    posts.comment_count           ,
    posts.comment_status          ,
    posts.post_excerpt            ,
    users.display_name AS `author`
FROM wp_posts AS `posts`
    LEFT JOIN wp_users AS `users`
        ON posts.post_author = users.ID
WHERE
    posts.post_status !='auto-draft' and
    posts.post_type in ('post', 'page')"""
)


def dump_posts(db, host, username, password, posts_output_dir, pages_output_dir):
    """
    Connects to the database and dumps the posts.

    Parameters
    ----------
    db : string
        Database name, such as wp_li3huo
    host : string
        Database host, such as localhost
    posts_output_dir : string
        WP posts as mardown files, such as ./posts
    pages_output_dir : string
        WP pages as mardown files, such as ./pages
    """

    os.makedirs(posts_output_dir, exist_ok=True)
    os.makedirs(pages_output_dir, exist_ok=True)

    # https://docs.sqlalchemy.org/en/20/dialects/mysql.html
    db = create_engine(f"mysql+mysqlconnector://{username}:{password}@{host}/{db}")

    post_categories, post_tags = _get_taxonomy(db)

    with db.engine.connect() as conn:
        for post in conn.execute(SQL_GET_POST):
            if post.post_type == "post":
                dump_single_post(post, post_categories, post_tags, posts_output_dir)
            elif post.post_type == "page":
                dump_single_page(post, pages_output_dir)


def main():
    """
    Script entry point
    """

    if len(sys.argv) < 6:
        print(USAGE.format(sys.argv[0]))
        return 0

    dump_posts(*sys.argv[1:])


if __name__ == "__main__":
    main()
