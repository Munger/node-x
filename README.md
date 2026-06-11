# Node-X

A composable graph object model for Python — serialisable, streamable, and
thread-safe — with zero core dependencies.

Tree libraries are common. Node-X is something else: a complete object model
for building, transmitting, and operating on typed graphs at runtime. A node
graph can be constructed, walked, mutated under concurrent access, frozen into
an immutable snapshot, serialised to JSON or YAML, sent over any wire — REST,
sockets, queues, whatever you have — and restored on the other end as a fully
typed, immediately operational object graph. Restored graphs can be grafted
onto live trees, streamed for lazy population, or frozen as trust boundaries.
The core library ships as a single file with no dependencies beyond the Python
standard library.

```python
from node_x import Node, NodeList, Serialisable, GraphMixin, NodeTransaction, ReadWriteMixin
```

---

## Contents

- [Installation](#installation)
- [Core concepts](#core-concepts)
- [Node](#node)
- [NodeList](#nodelist)
- [Subclassing](#subclassing)
- [Tree walking](#tree-walking)
- [Freeze and thaw](#freeze-and-thaw)
- [Merge](#merge)
- [Serialisation](#serialisation)
- [Graph identity](#graph-identity)
- [YAML](#yaml)
- [Streaming](#streaming)
- [Thread safety](#thread-safety)
- [Class reference](#class-reference)

---

## Installation

```
pip install node-x
```

For YAML serialisation support:

```
pip install node-x[yaml]
```

Or drop `node_x.py` directly into your project — it has no dependencies
beyond the Python standard library.

---

## Core concepts

A `Node` is a `dict` whose values are constrained to a safe whitelist:

| Allowed | Rejected |
|---|---|
| `Node`, `NodeList` | raw `list` — use `NodeList` |
| `str`, `int`, `float`, `bool`, `bytes`, `None` | raw `dict` — wrap in a `Node` subclass |
| `tuple` (recursively validated) | any other type |

Attribute access is transparently routed to the dict payload, so `node.foo` and
`node["foo"]` are equivalent. Names that shadow methods or class attributes are
detected automatically and blocked.

---

## Node

### Construction

```python
from node_x import Node

# Empty node
n = Node()

# From a dict
n = Node({"title": "Hello", "count": 0})

# Keyword arguments
n = Node(title="Hello", count=0)

# Mixed
n = Node({"title": "Hello"}, count=0)
```

### Getting and setting values

```python
n = Node()

# Dict-style
n["title"] = "Hello"
print(n["title"])        # Hello

# Attribute-style — both read and write
n.title = "World"
print(n.title)           # World

# All standard dict methods work
n.update({"a": 1, "b": 2})
n.pop("a")
n.clear()
```

### Value safety

Node rejects values that bypass its safety guarantees:

```python
n = Node()

n["tags"] = ["python"]        # TypeError — use NodeList
n["meta"] = {"k": "v"}        # TypeError — wrap in a Node subclass

n["tags"] = ("python",)       # OK — tuples are allowed (immutable)
```

---

## NodeList

`NodeList` is a thread-safe `list` that only accepts `Node` instances. It
mirrors the full `list` API with locking and type validation on every mutation.

```python
from node_x import Node, NodeList

items = NodeList()
items.append(Node({"id": 1, "name": "alpha"}))
items.append(Node({"id": 2, "name": "beta"}))

items.sort(key=lambda n: n["id"])
items.reverse()

# Slice assignment
items[0:1] = [Node({"id": 99})]

# NodeList cannot contain plain values or other NodeLists
items.append("string")   # TypeError
items.append(NodeList()) # TypeError — NodeList is not a Node
```

---

## Subclassing

The real power of Node-X emerges when you define subclasses. Method names,
properties, and annotated fields are automatically reserved — you never need to
maintain a manual exclusion list.

```python
from node_x import Node, NodeList

class Document(Node):
    _children = ("tags",)       # field names that hold child Node/NodeList

    @property
    def word_count(self) -> int:
        return len(self.get("body", "").split())

    def summary(self) -> str:
        return f"{self['title']} ({self.word_count} words)"
```

```python
doc = Document({"title": "Node-X Guide", "body": "Thread safe and powerful."})
print(doc.word_count)   # 4
print(doc.summary())    # Node-X Guide (4 words)

# "word_count" and "summary" are reserved — they cannot be set as payload keys
doc["word_count"] = 99  # KeyError: 'word_count' is reserved on Document
```

### Property-backed payload keys

When a property reads from the dict, `__setitem__` allows writes through it:

```python
class Config(Node):
    @property
    def debug(self) -> bool:
        return self.get("debug", False)

    @debug.setter
    def debug(self, value: bool) -> None:
        self["debug"] = value

cfg = Config()
cfg.debug = True
print(cfg["debug"])   # True
```

### Typed annotations as reserved names

```python
class TypedNode(Node):
    name: str       # "name" is reserved and cannot be used as a payload key
    count: int      # same for "count"
```

---

## Tree walking

Define `_children` on a subclass to tell Node-X which payload fields hold the
structural children of the tree. Two methods then become available:

### `_tree_iter()`

Depth-first generator yielding every node in the subtree, starting with `self`.

```python
class Folder(Node):
    _children = ("files",)

class File(Node):
    pass

root = Folder({"name": "root"})
root["files"] = NodeList([
    File({"name": "a.txt"}),
    File({"name": "b.txt"}),
])

for node in root._tree_iter():
    print(node.get("name"))
# root
# a.txt
# b.txt
```

`_children` may name either a `NodeList` field or a single `Node` field:

```python
class Article(Node):
    _children = ("author",)   # author is a single Node, not a list

class Person(Node):
    pass

article = Article({"title": "Deep Dive"})
article["author"] = Person({"name": "Alice"})

list(article._tree_iter())   # [article, person]
```

### `_walk_child_nodes()`

Apply a callable to every descendant Node, with an optional second callable
for NodeList containers:

```python
def mark_visited(node: Node) -> None:
    node["visited"] = True

def log_list(nl: NodeList) -> None:
    print(f"  traversing list of {len(nl)}")

root._walk_child_nodes(mark_visited, list_func=log_list)
```

---

## Freeze and thaw

Freeze makes a node (and optionally its entire subtree) immutable. Every
mutation method raises `TypeError` until `thaw()` is called.

```python
config = Node({"host": "localhost", "port": 5432})
config.freeze()

config["host"] = "remotehost"   # TypeError: Cannot set key 'host' on frozen Node.
                                 #            Call thaw() to restore mutability.

config.thaw()
config["host"] = "remotehost"   # OK
```

### Deep vs shallow

```python
child = Node({"x": 1})
parent = Node()
parent["child"] = child

parent.freeze(deep=True)   # freezes parent AND child
child["x"] = 2             # TypeError

parent.freeze(deep=False)  # freezes parent only
child["x"] = 2             # OK — child still mutable
```

Deep freeze also reaches `Node` instances inside `NodeList` containers.

```python
items = NodeList([Node({"v": 1})])
parent = Node({"items": items})
parent.freeze()         # propagates into the NodeList and its elements

items.append(Node())    # TypeError
items[0]["v"] = 2       # TypeError

parent.thaw()           # restores everything
```

---

## Merge

`merge()` recursively merges another mapping into a node. Matching Node fields
are merged recursively; scalars and NodeLists are overwritten. Returns `self`
for chaining.

```python
defaults = Node({"timeout": 30, "retries": 3, "options": Node({"verbose": False})})
overrides = {"timeout": 60, "options": Node({"verbose": True})}

defaults.merge(overrides)
print(defaults["timeout"])              # 60   — scalar overwritten
print(defaults["retries"])              # 3    — untouched
print(defaults["options"]["verbose"])   # True — nested Node merged recursively
```

```python
# Chaining
node = Node({"a": 1}).merge({"b": 2}).merge({"c": 3})
```

---

## Serialisation

Mix in `Serialisable` to add snapshot/restore/clone to any Node subclass.

```python
from node_x import Node, NodeList, Serialisable, SerialisableNodeList
import json

class Tag(Serialisable, Node):
    pass

class Article(Serialisable, Node):
    _children = ("tags",)
```

### Snapshot and restore

```python
article = Article({"title": "Deep Dive", "views": 1200})
article["tags"] = SerialisableNodeList([Tag({"name": "python"})])

# Snapshot to plain Python (JSON-safe)
snap = article.snapshot()
# {'title': 'Deep Dive', 'views': 1200, 'tags': [{'name': 'python'}]}
#  ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^  scalars first, nested after

# Pretty JSON
print(article.to_pretty_json())

# Restore from snapshot
restored = Article.restore(snap)
print(restored["title"])   # Deep Dive
```

### Disk round-trip

For nodes with typed children, define a custom `restore()` that uses
`_from_payload` + `_restore_children`:

```python
class Article(Serialisable, Node):
    _node_fields  = {"author": Person}
    _list_fields  = {"tags": (SerialisableNodeList, Tag)}
    _restore_via_payload = True

    @classmethod
    def restore(cls, snapshot):
        node = cls._from_payload(snapshot)
        cls._restore_children(node, snapshot)
        return node
```

```python
# Write to disk
snap = article.snapshot()
with open("article.json", "w") as f:
    json.dump(snap, f, indent=2)

# Read back
with open("article.json") as f:
    data = json.load(f)

restored = Article.restore(data)
print(isinstance(restored["author"], Person))   # True
print(isinstance(restored["tags"], SerialisableNodeList))  # True
```

### Clone

`clone()` deep-copies the entire subtree without calling `__init__`, so
subclass constructors with required arguments remain valid on the clone.
Shared Node references within the tree are preserved via a memo dict.

```python
original = Article({"title": "Original"})
copy = original.clone()

copy["title"] = "Copy"
print(original["title"])   # Original — unaffected
```

---

## Graph identity

Mix in `GraphMixin` to give a Node subclass a class-level registry. Every call
to `get_or_create()` with the same key returns the *same* Python object,
regardless of how many traversal paths discover it. This is what makes a true
graph rather than a tree: a node shared by multiple parents is one object in
memory, not one copy per parent.

```python
from node_x import Node, GraphMixin

class PersonNode(GraphMixin, Node):
    pass

a = PersonNode.get_or_create("user-42", {"name": "Alice"})
b = PersonNode.get_or_create("user-42")

assert a is b           # True — same instance every time
assert a["name"] == "Alice"
```

Keys must be strings — they are stored as `_key` in the node payload and used
as `$ref` targets during serialisation.

### Graph-aware serialisation

`Serialisable.to_plain()` understands `GraphMixin` nodes. The first time a
keyed node is encountered in a depth-first walk it is emitted in full; every
subsequent reference becomes a `{"$ref": key}` marker. `restore()` resolves
all `$ref` markers back to the original shared instance, preserving true graph
identity across serialisation round-trips.

```python
from node_x import Node, GraphMixin, Serialisable, SerialisableNodeList

class PersonNode(GraphMixin, Serialisable, Node):
    _restore_via_payload = True

class DocumentNode(Serialisable, Node):
    _restore_via_payload = True
    _node_fields = {"author": PersonNode}

class Corpus(Serialisable, Node):
    _restore_via_payload = True
    _list_fields = {"documents": (SerialisableNodeList, DocumentNode)}

PersonNode.clear_registry()
alice = PersonNode.get_or_create("user-42", {"name": "Alice"})

doc_a = DocumentNode({"title": "Introduction"})
doc_b = DocumentNode({"title": "Advanced Topics"})
doc_a["author"] = alice
doc_b["author"] = alice   # same object — one author, two documents

corpus = Corpus({})
corpus["documents"] = SerialisableNodeList([doc_a, doc_b])

# Second author reference becomes {"$ref": "user-42"} in the snapshot
snap = corpus.snapshot()

# Restore — both documents point to the same PersonNode instance
restored = Corpus.restore(snap)
docs = restored["documents"]
assert docs[0]["author"] is docs[1]["author"]   # True
```

### Visited flag

`is_known` / `mark_known()` provide a lightweight BFS visited flag, useful
when streaming a graph and you need to avoid expanding a node more than once:

```python
node = PersonNode.get_or_create("user-42")

if not node.is_known:
    node.mark_known()
    for child in node.stream():
        ...
```

Call `clear_registry()` between independent runs to prevent stale instances
from a previous session being returned:

```python
PersonNode.clear_registry()
```

---

## YAML

`node_x_yaml` is a companion module (included in the package, requires
`pip install node-x[yaml]`) that serialises node trees to YAML and back.

```python
import node_x_yaml

# Serialise
text = node_x_yaml.dump(timeline)

# Restore
restored = node_x_yaml.load(Timeline, text)
```

`dump()` defaults to block YAML (one key per line, human-readable and
diffable). Pass `default_flow_style=True` for compact inline notation.
Non-ASCII characters are written as literal UTF-8 — no escape sequences.

`$ref` graph markers are round-tripped correctly: a shared node serialised
with `{"$ref": "key"}` in the YAML restores to a single shared Python object,
preserving graph identity exactly as JSON does.

```python
# Block style (default) — readable, diffable
text = node_x_yaml.dump(node)

# Inline style — compact
text = node_x_yaml.dump(node, default_flow_style=True)
```

---

## Streaming

`StreamMixin` adds lazy child discovery to a Node. Override `stream()` to
yield children on demand — they are constructed only as the caller iterates,
making it ideal for large or remote data sources.

```python
from node_x import Node, StreamMixin

class DirectoryNode(StreamMixin, Node):
    def stream(self, data=None):
        import os
        for entry in os.scandir(self["path"]):
            yield FileNode({"name": entry.name, "path": entry.path,
                            "is_dir": entry.is_dir()})

class FileNode(StreamMixin, Node):
    def stream(self, data=None):
        # Leaf nodes yield nothing by default
        return
        yield
```

```python
root = DirectoryNode({"path": "/tmp"})

# Children are created only as you iterate — no upfront scan
for entry in root.stream():
    print(entry["name"])
```

### Data-driven streaming

Pass pre-fetched bytes to avoid repeated I/O:

```python
class CsvNode(StreamMixin, Node):
    def stream(self, data=None):
        if data is None:
            return
        for line in data.decode().splitlines():
            parts = line.split(",")
            yield Node({"key": parts[0], "value": parts[1]})

node = CsvNode()
payload = b"a,1\nb,2\nc,3\n"

for child in node.stream(data=payload):
    print(child["key"], child["value"])
```

### Cascading and tree-walk pattern

Combine `_tree_iter()` with `stream()` to populate a tree level by level:

```python
class Container(StreamMixin, Node):
    _children = ("sections",)   # static children

    def stream(self, data=None):
        # dynamic children discovered from payload
        for name in self.get("items", ()):
            yield Item({"name": name})

class Item(StreamMixin, Node):
    pass

root = Container({"items": ("a", "b")})
sub  = Container({"items": ("c",)})
root["sections"] = NodeList([sub])

all_items = []
for node in root._tree_iter():        # walk static skeleton
    for item in node.stream():        # discover dynamic children
        all_items.append(item)

print([i["name"] for i in all_items])  # ['a', 'b', 'c']
```

### Laziness and early exit

Generators are not evaluated until consumed. Breaking early incurs no cost
for the unconsumed tail:

```python
for child in huge_node.stream():
    if matches(child):
        process(child)
        break   # remaining children are never constructed
```

---

## Thread safety

### What is protected automatically

All Node and NodeList mutations — `__setitem__`, `__delitem__`, `update`,
`pop`, `clear`, `append`, `extend`, `sort`, etc. — acquire the per-instance
`RLock` before modifying state. Subclasses inherit this protection for any
write that goes through the normal attribute or item assignment path.

### What callers must synchronise

**Reads are not protected.** In CPython the GIL makes isolated reads
practically safe, but a read-modify-write sequence is not atomic:

```python
# NOT safe under concurrent writes
v = node["counter"]
node["counter"] = v + 1

# Safe — hold the node's own lock explicitly
with node.lock:
    node["counter"] = node["counter"] + 1
```

### Cross-node operations — `NodeTransaction`

When you need to read from one node and write to another atomically, use
`NodeTransaction`. It acquires all locks in `id()`-sorted order, which
prevents ABBA deadlock regardless of how many concurrent transactions are
in flight.

```python
from node_x import Node, NodeTransaction

account_a = Node({"balance": 1000})
account_b = Node({"balance": 500})

def transfer(src, dst, amount):
    with NodeTransaction(src, dst):
        if src["balance"] >= amount:
            src["balance"] -= amount
            dst["balance"] += amount

# Safe to call concurrently from multiple threads
import threading
threads = [
    threading.Thread(target=transfer, args=(account_a, account_b, 100))
    for _ in range(10)
]
for t in threads:
    t.start()
for t in threads:
    t.join()
```

`NodeTransaction` works with any mix of `Node` and `NodeList` instances.

### Safe iteration — `ReadWriteMixin`

By default, iterating a NodeList while another thread mutates it is not
protected. Mix in `ReadWriteMixin` to get a `reading()` context that
blocks writers until iteration completes — with no changes required in
writer code:

```python
from node_x import Node, NodeList, ReadWriteMixin

class LiveList(ReadWriteMixin, NodeList):
    pass

class LiveNode(ReadWriteMixin, Node):
    pass

queue = LiveList()
queue.append(Node({"job": "build"}))

# Reader thread
with queue.reading():
    for item in queue:          # writers block until this exits
        process(item)

# Writer thread — no changes needed; blocks transparently if reader is active
queue.append(Node({"job": "deploy"}))
```

Multiple concurrent readers are allowed simultaneously. The write lock is
re-entrant for the same thread, so methods that call other mutating methods
internally (such as `merge()` calling `__setitem__`) do not deadlock.

`ReadWriteMixin` is compatible with both `Node` and `NodeList`, and with
`NodeTransaction`:

```python
class SafeNode(ReadWriteMixin, Node):
    pass

a = SafeNode({"x": 1})
b = SafeNode({"y": 2})

with NodeTransaction(a, b):
    a["x"] = b["y"]   # safe: write guards acquired for both nodes
```

---

## Class reference

| Class | Bases | Purpose |
|---|---|---|
| `Node` | `dict` | Thread-safe dict-backed node; per-instance `RLock`, payload validation, freeze/thaw, merge, tree walking |
| `NodeList` | `list` | Thread-safe Node-only collection; mirrors full `list` API |
| `GraphMixin` | — | Per-class registry for true graph identity; `get_or_create()`, `clear_registry()`, `is_known`, `mark_known()` |
| `StreamMixin` | — | Adds `stream(data=None)` virtual method for lazy child discovery |
| `Serialisable` | — | Mixin adding `snapshot()`, `restore()`, `clone()`, `to_plain()`, `to_pretty_json()`; graph-aware `$ref` serialisation when combined with `GraphMixin` |
| `SerialisableNodeList` | `NodeList` | NodeList with `snapshot()`, `restore()`, `to_pretty_json()` |
| `NodeTransaction` | — | Context manager; acquires multiple Node/NodeList locks in deadlock-free order |
| `ReadWriteMixin` | — | Opt-in readers-writer lock; `reading()` context blocks writers during safe iteration |

### node_x_yaml

| Function | Purpose |
|---|---|
| `dump(node, *, default_flow_style=False, indent=2)` | Serialise a `Serialisable` node tree to a YAML string |
| `load(cls, text)` | Reconstruct a node tree from a YAML string via `cls.restore()` |

### Payload type whitelist

`Node` and `NodeList` enforce this whitelist on every write:

```
Node · NodeList · str · int · float · bool · bytes · None · tuple (recursively validated)
```

Raw `list` → use `NodeList`. Raw `dict` → wrap in a `Node` subclass.

---

## License

MIT
