"""Lazy, CPU-only dependency parsing with content-addressed model provenance.

Missing/incompatible NLP is explicit: it never invokes a lexical fallback.
Importing this module neither imports spaCy nor loads/invokes a model.
"""

from collections import OrderedDict
from contextlib import contextmanager
import errno
import hashlib
from importlib import metadata
import os
from pathlib import Path
import threading
import time

from .settings import Settings

EXPECTED = {
    "name": "en_core_sci_sm",
    "version": "0.5.4",
    "spacy_version": "3.7.5",
    "scispacy_version": "0.6.2",
}
MODEL_URL = "https://s3-us-west-2.amazonaws.com/ai2-s2-scispacy/releases/v0.5.4/en_core_sci_sm-0.5.4.tar.gz"


class ParserUnavailable(RuntimeError):
    """Parser availability is a processing failure, not a completed extraction."""


@contextmanager
def cpu_inference_lock(path=None):
    """Serialize actual forward passes across CLI and worker processes.

    The lock file persists; OS advisory ownership disappears on process exit,
    including forced termination. Isolated benchmarks must set the same
    LITERATURE_NLP_LOCK as the production worker to share the CPU budget.
    """
    target = Path(
        path or os.environ.get("LITERATURE_NLP_LOCK") or Settings.from_env().home / "locks" / "nlp-cpu.lock"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a+b") as stream:
        if os.name == "posix":
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                yield target
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        elif os.name == "nt":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise ParserUnavailable(f"CPU advisory lock failed: {error}") from error
                    time.sleep(0.05)
            try:
                yield target
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            raise ParserUnavailable(f"No supported OS advisory CPU lock for {os.name}")


class CpuParser:
    def __init__(self):
        self._nlp = None
        self._manifest = {**EXPECTED, "available": False, "mode": "production", "model_sha256": None}
        self._lock = threading.RLock()
        self._cache = OrderedDict()

    def _load(self):
        if self._nlp is not None:
            return
        try:
            actual = {
                "spacy_version": metadata.version("spacy"),
                "scispacy_version": metadata.version("scispacy"),
                "version": metadata.version(EXPECTED["name"]),
            }
            for name, value in actual.items():
                if value != EXPECTED[name]:
                    raise ParserUnavailable(f"incompatible {name}: {value}; expected {EXPECTED[name]}")
            import spacy

            package = __import__(EXPECTED["name"])
            package_root = Path(package.__file__).resolve().parent
            digest = hashlib.sha256()
            # Hash weights/vocabulary/config, not only metadata. Generated caches
            # are excluded; package-relative names are stable across deployment.
            for path in sorted(package_root.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    digest.update(path.relative_to(package_root).as_posix().encode() + b"\0")
                    with path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1048576), b""):
                            digest.update(chunk)
            nlp = spacy.load(EXPECTED["name"], disable=["ner"])
            if "parser" not in nlp.pipe_names or nlp.meta.get("version") != EXPECTED["version"]:
                raise ParserUnavailable("model lacks the pinned dependency parser")
            self._manifest = {
                **EXPECTED,
                "available": True,
                "mode": "production",
                "model_sha256": digest.hexdigest(),
                "model_url": MODEL_URL,
                "pipeline": nlp.pipe_names,
                "device": "cpu",
            }
            self._nlp = nlp
        except ParserUnavailable:
            raise
        except Exception as error:
            self._manifest["error"] = f"{type(error).__name__}: {error}"
            raise ParserUnavailable(self._manifest["error"]) from error

    @property
    def manifest(self):
        return dict(self._manifest)

    def parse(self, sentence):
        with self._lock:
            self._load()
            line = sentence["text"]
            if len(line) > 30000:
                raise ParserUnavailable("sentence exceeds 30000-codepoint parser limit; retained for retry")
            key = hashlib.sha256(line.encode()).hexdigest()
            if key not in self._cache:
                with cpu_inference_lock():
                    doc = self._nlp(line)
                self._cache[key] = [
                    {
                        "i": token.i,
                        "text": token.text,
                        "start": token.idx,
                        "end": token.idx + len(token.text),
                        "lemma": token.lemma_.casefold(),
                        "dep": token.dep_,
                        "head": token.head.i,
                        "pos": token.pos_,
                    }
                    for token in doc
                ]
                if len(self._cache) > 256:
                    self._cache.popitem(last=False)
            self._cache.move_to_end(key)
            offset = sentence["start"]
            return [
                {**token, "start": token["start"] + offset, "end": token["end"] + offset}
                for token in self._cache[key]
            ]


_PARSER = CpuParser()


def parser_for(metadata_value):
    parser = metadata_value.get("_parser") or _PARSER
    return parser


def parser_manifest(parser):
    manifest = dict(getattr(parser, "manifest", {}))
    if not isinstance(parser, CpuParser):
        # Injection is for reproducible synthetic tests; it cannot pass a
        # production promotion gate even when a fixture claims otherwise.
        manifest.update(
            {
                "mode": "fixture",
                "available": manifest.get("available", True),
                "name": manifest.get("name", "injected-dependency-fixture"),
            }
        )
    return manifest


def validate_tokens(tokens, sentence):
    indexes = {token.get("i") for token in tokens}
    if indexes != set(range(len(tokens))):
        raise ParserUnavailable("invalid dependency token indexes")
    for token in tokens:
        if token.get("head") not in indexes or not (
            sentence["start"] <= token.get("start", -1) < token.get("end", -1) <= sentence["end"]
        ):
            raise ParserUnavailable("dependency token outside exact source sentence")
        if (
            sentence["text"][token["start"] - sentence["start"] : token["end"] - sentence["start"]]
            != token["text"]
        ):
            raise ParserUnavailable("dependency token text differs from immutable source")
    return tokens
