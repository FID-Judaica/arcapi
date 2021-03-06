import asyncio
import concurrent.futures
import functools
import json
import tornado.web

from arc import picaqueries, filters, solrtools
from arc.decode import debracket
import arc.config
import deromanize
from deromanize.keygenerator import CombinatorialExplosion
import string
from arc.nlitools import solrmarc
from typing import Mapping, Sequence, NamedTuple


nli_template = "https://www.nli.org.il/en/books/NNL_ALEPH{}/NLI"


def getquery(words):
    words = filter(None, [s.strip(string.punctuation) for s in words])
    return solrtools.join(words, fuzzy=True)


empty = object()


def getter(func):
    result = empty

    @functools.wraps(func)
    def wrapper():
        nonlocal result
        if result is empty:
            result = func()
        return result

    return wrapper


@getter
def getsession() -> arc.config.Session:
    session = arc.config.Session.fromconfig(asynchro=True)
    session.records.session.connection().engine.dispose()
    session.add_decoders(("new", "old", "pi"), fix_numerals=True)
    session.add_core("nlibooks")
    session.add_termdict()
    return session


@getter
def getpool() -> concurrent.futures.ProcessPoolExecutor:
    return concurrent.futures.ProcessPoolExecutor()


def parallel(func, *args):
    return asyncio.get_event_loop().run_in_executor(getpool(), func, *args)


jsondecode = json.JSONDecoder().decode
jsonencode = json.JSONEncoder(ensure_ascii=False).encode


def split_title(text: str):
    remainder, _, resp = text.partition(" / ")
    main, _, sub = remainder.partition(" : ")
    return main, sub, resp


def mk_rlist_serializable(rlist: deromanize.ReplacementList):
    reps = [str(rep) for rep in rlist[:30]]
    key = rlist.key if isinstance(rlist, deromanize.ReplacementList) else rlist
    return dict(key=key, reps=reps)


def text_to_replists(text):
    if not text:
        return []
    chunks = getsession().getchunks(text)
    rlists = picaqueries.prerank(chunks, getsession())
    return [mk_rlist_serializable(rl) for rl in rlists]


def title_to_replists(title: str):
    main, sub, resp = map(text_to_replists, split_title(title))
    main += sub
    main += resp
    return main


def has_heb(string: str):
    line = filters.Line(debracket(string))
    if not (line.has("only_old") or line.has("only_new")):
        return False
    if line.has("foreign", "yiddish_ending", "arabic_article", "english_y"):
        return False
    return True


def person_to_replists(person: str):
    if not has_heb(person):
        return None
    return text_to_replists(person)


def ppn2record_and_rlist(ppn):
    record = getsession().records[ppn]
    title = picaqueries.gettranstitle(record)
    serializable_rlists = title_to_replists(title.joined)
    return dict(record=record.to_dict(), replists=serializable_rlists)


async def query_nli(words):
    try:
        query = getquery(words)
    except solrtools.EmptyQuery:
        return []
    out = await getsession().cores.nlibooks.run_query(
        "alltitles:" + query, fl=["originalData"]
    )
    return [jsondecode(d["originalData"]) for d in out["docs"]]


class MalformedRecord(Exception):
    pass


class NoTitleGiven(Exception):
    pass


def prep_record(record: dict):
    # mutation
    for k, v in record.items():
        if isinstance(v, str):
            record[k] = [v]
        elif not isinstance(v, list):
            raise MalformedRecord(record)


title_t = "title"
isPartOf_t = "isPartOf"


def gettitle(record):
    try:
        title = record["title"][0]
        if not title:
            raise NoTitleGiven(record)
        return (title_t, title)
    except (KeyError, IndexError):
        try:
            title = record["isPartOf"][0]
            if not title:
                raise NoTitleGiven(record)
            return (isPartOf_t, title)
        except (KeyError, IndexError):
            raise NoTitleGiven(record)


class TitleReplists(NamedTuple):
    type: str
    replists: dict


def record2replist(record: Mapping[str, Sequence[str]]):
    prep_record(record)
    title_type, title = gettitle(record)
    title_replists = title_to_replists(title)
    creator_replists = map(person_to_replists, record.get("creator", []))
    return (TitleReplists(title_type, title_replists), list(creator_replists))


def json_records2replists(json_records: str):
    records = jsondecode(json_records)
    out = []
    for record in records:
        try:
            out.append((record, record2replist(record)))
        except (NoTitleGiven, CombinatorialExplosion) as e:
            out.append((record, e))
    return out


def words_of_replists(replists):
    return [w["reps"][0] for w in replists]


def error(msg, record, **kwargs):
    return {"error": msg, "record": record, **kwargs}


async def record_with_results(record, replists_or_error):
    if isinstance(replists_or_error, Exception):
        return error(replists_or_error.__class__.__name__, record)
    (title_type, replists), creator_replists = replists_or_error
    words = words_of_replists(replists)
    results = await query_nli(words)
    # for result in results:
    #     title = solrmarc.gettitle(result)
    results = await parallel(
        solrmarc.rank_results,
        record.get("creator", []),
        record.get("date", []),
        [x["reps"] for x in replists],
        results,
    )

    if not results:
        return error("no matches found", record, best_guess=" ".join(words))

    results = [r["doc"] for r in results]
    title = picaqueries.Title(*solrmarc.gettitle(results[0])).text
    heb_title = title.replace("<<", "{").replace(">>", "}")
    record[title_type].append(heb_title)
    for result in results:
        record.setdefault("relation", []).append(
            nli_template.format(result["controlfields"]["001"])
        )
    return record


class APIHandler(tornado.web.RequestHandler):
    async def get(self, json_records):
        records_n_replists = await parallel(
            json_records2replists, json_records
        )
        sep_sym = "["
        for record, replists_or_error in records_n_replists:
            self.write(sep_sym)
            self.write(
                jsonencode(
                    await record_with_results(record, replists_or_error)
                )
            )
            sep_sym = "\n,"
        self.write("\n]")


class PPNHandler(tornado.web.RequestHandler):
    async def get(self, ppn):
        try:
            result = await parallel(ppn2record_and_rlist, ppn)
        except KeyError:
            self.write(
                jsonencode({"Error": "No such PPN %s", "type": "PPNError"})
                % ppn
            )
            return

        self.write(jsonencode(result))


class TextHandler(tornado.web.RequestHandler):
    async def get(self, text):
        result = await parallel(text_to_replists, text)
        self.write(jsonencode(result))


class NLIQueryHandler(tornado.web.RequestHandler):
    async def get(self, data):
        out = await query_nli(jsondecode(data))
        self.write(jsonencode(out))


class TextAndQueryHandler(tornado.web.RequestHandler):
    async def get(self, text):
        text = await parallel(text_to_replists, text)
        words = words_of_replists(text)
        results = await query_nli(words)
        self.write(jsonencode({"conversion": text, "matches": results}))


class NextHandler(tornado.web.RequestHandler):
    async def get(self):
        from arcapi.ppns import ppns

        self.write(next(ppns))


class PassHandler(tornado.web.RequestHandler):
    async def get(self, ppn):
        from arcapi.ppns import ppns

        ppns[ppn] = "null"
        self.write(next(ppns))


class SubmitHandler(tornado.web.RequestHandler):
    async def get(self, hairball):
        from arcapi.ppns import ppns

        data = jsondecode(hairball)
        ppns[data["ppn"]] = hairball
        self.write(next(ppns))
