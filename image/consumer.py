import logging
import subprocess
import textwrap
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from fedora_messaging import config
from fedora_messaging.exceptions import Drop, Nack

log = logging.getLogger(__name__)


class GitReceiveConsumer:

    def __init__(self):
        cfg = config.conf["consumer_config"]
        self.public_inbox_dir = cfg.get("public_inbox_dir", "/var/lib/public-inbox")
        self.list_address = cfg.get("list_address", "git-commits@fedoraproject.org")
        log.info(
            "GitReceiveConsumer initialised (public-inbox dir: %s, list address: %s)",
            self.public_inbox_dir,
            self.list_address,
        )

    def __call__(self, message):
        try:
            email = self._build_email(message)
        except Exception:
            log.exception("Failed to build email from message %s", message.id)
            raise Drop("Could not parse message body")

        try:
            self._inject(email)
        except subprocess.CalledProcessError as e:
            log.error("public-inbox-mda failed (exit %d): %s", e.returncode, e.stderr)
            raise Nack()

    def _build_email(self, message):
        commit = message.body.get("commit", {})

        repo      = commit.get("repo", "unknown")
        namespace = commit.get("namespace", "")
        branch    = commit.get("branch", "unknown")
        rev       = commit.get("rev", "")
        summary   = commit.get("summary", "(no summary)")
        msg_body  = commit.get("message", "")
        patch     = commit.get("patch", "")
        url       = commit.get("url", "")
        author    = commit.get("name", "Unknown")
        email_    = commit.get("email", self.list_address)
        date      = commit.get("date", "")

        full_repo = f"{namespace}/{repo}" if namespace else repo

        subject = f"[{full_repo}] {branch}: {summary}"

        stats = commit.get("stats", {}).get("total", {})
        stats_line = (
            f"+{stats.get('additions', 0)}/-{stats.get('deletions', 0)} "
            f"in {stats.get('files', 0)} file(s)"
        )

        body_text = textwrap.dedent(f"""\
            A new commit has been pushed.

            Repo   : {full_repo}
            Branch : {branch}
            Commit : {rev}
            Author : {author} <{email_}>
            Date   : {date}
            Stats  : {stats_line}
            URL    : {url}

            Log:
            {msg_body.strip()}
        """)

        if patch:
            body_text += f"\n---\n{patch}"

        msg = MIMEText(body_text, "plain", "utf-8")
        msg["From"] = f"{author} <{email_}>"
        msg["To"] = self.list_address
        msg["Subject"] = subject
        msg["Date"] = formatdate(usegmt=True)
        msg["Message-ID"] = make_msgid(
            idstring=f"{full_repo.replace('/', '-')}-{rev[:12]}",
            domain="fedoraproject.org",
        )
        msg["List-ID"] = f"<{self.list_address.replace('@', '.')}>"
        msg["X-Git-Repo"] = full_repo
        msg["X-Git-Branch"] = branch
        msg["X-Git-Rev"] = rev

        return msg.as_string()

    def _inject(self, email_str):
        import os
        env = os.environ.copy()
        env["ORIGINAL_RECIPIENT"] = self.list_address
        env["HOME"] = self.public_inbox_dir
        subprocess.run(
            ["public-inbox-mda", "--no-precheck"],
            input=email_str,
            env=env,
            text=True,
            capture_output=True,
            check=True,
        )
        log.info("Injected commit into public-inbox")
