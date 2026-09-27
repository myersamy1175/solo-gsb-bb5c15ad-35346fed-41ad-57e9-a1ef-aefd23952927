"""manifest-audit：约定清单与交付清单核对测试（纯内存，只比较声明摘要）。"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

H1 = "a" * 64
H2 = "b" * 64
H3 = "c" * 64


def _audit(agreed, delivered):
    r = client.post(
        "/api/v1/manifest-audit", json={"agreed": agreed, "delivered": delivered}
    )
    assert r.status_code == 200, r.text
    return r.json()


def _refs(items):
    """取条目引用列表的 (side, index)。"""
    return [(e["side"], e["index"]) for e in items]


def _pairs(items):
    return [
        (p["status"], p["comparison_key"], p["agreed"]["index"], p["delivered"]["index"])
        for p in items
    ]


def _file(path, sha=H1):
    return {"path": path, "type": "file", "sha256": sha}


def _dir(path):
    return {"path": path, "type": "directory"}


# ---------------------------------------------------------------------------
# 一致 / 缺失 / 额外
# ---------------------------------------------------------------------------


def test_match_missing_extra_and_summary():
    body = _audit(
        agreed=[
            _file("docs/readme.txt", H1),
            _file("docs/only-agreed.txt", H1),
            _dir("assets"),
        ],
        delivered=[
            _file("docs/readme.txt", H1),
            _file("docs/only-delivered.txt", H1),
            _dir("assets"),
        ],
    )
    assert body["target"] == "windows"
    assert _pairs(body["matched"]) == [
        ("MATCH", "assets", 3, 3),
        ("MATCH", "docs/readme.txt", 1, 1),
    ]
    assert _refs(body["missing"]) == [("agreed", 2)]
    assert _refs(body["extra"]) == [("delivered", 2)]
    assert body["type_mismatches"] == []
    assert body["digest_mismatches"] == []
    assert body["hierarchy_blocked"] == []
    assert body["ambiguous_keys"] == []
    assert body["summary"] == {
        "agreed_total": 3,
        "delivered_total": 3,
        "matched": 2,
        "missing": 1,
        "extra": 1,
        "type_mismatches": 0,
        "digest_mismatches": 0,
        "hierarchy_blocked": 0,
        "ambiguous_keys": 0,
        "ambiguous_entries": 0,
    }


def test_empty_manifests_ok():
    body = _audit([], [])
    assert body["summary"]["agreed_total"] == 0
    assert body["summary"]["delivered_total"] == 0
    for key in (
        "matched",
        "missing",
        "extra",
        "type_mismatches",
        "digest_mismatches",
        "hierarchy_blocked",
        "ambiguous_keys",
    ):
        assert body[key] == []


# ---------------------------------------------------------------------------
# Windows 规范化沿用 + 摘要大小写不敏感
# ---------------------------------------------------------------------------


def test_windows_normalization_and_digest_casefold():
    body = _audit(
        agreed=[
            # 反斜杠、大小写、段尾点/空格差异在比较键上抹平。
            _file("Docs\\ReadMe.txt.", H1),
        ],
        delivered=[
            # 大写十六进制摘要归一为小写后应判一致。
            _file("docs/readme.txt", H1.upper()),
        ],
    )
    assert len(body["matched"]) == 1
    assert body["matched"][0]["comparison_key"] == "docs/readme.txt"
    assert body["matched"][0]["agreed"]["canonical_path"] == "Docs/ReadMe.txt"
    assert body["matched"][0]["agreed"]["sha256"] == H1
    assert body["matched"][0]["delivered"]["sha256"] == H1
    # 响应保留双方原始路径与原下标。
    assert body["matched"][0]["agreed"]["original_path"] == "Docs\\ReadMe.txt."
    assert body["matched"][0]["agreed"]["index"] == 1
    assert body["matched"][0]["delivered"]["index"] == 1


# ---------------------------------------------------------------------------
# 类型不符 / 摘要不符
# ---------------------------------------------------------------------------


def test_type_and_digest_mismatches():
    body = _audit(
        agreed=[_file("a.txt", H1), _dir("b"), _dir("c")],
        delivered=[_file("a.txt", H2), _file("b", H1), _dir("c")],
    )
    assert _pairs(body["type_mismatches"]) == [("TYPE_MISMATCH", "b", 2, 2)]
    # 目录对不比较摘要，一致；文件摘要不同才是 DIGEST_MISMATCH。
    assert _pairs(body["digest_mismatches"]) == [
        ("DIGEST_MISMATCH", "a.txt", 1, 1)
    ]
    assert _pairs(body["matched"]) == [("MATCH", "c", 3, 3)]


# ---------------------------------------------------------------------------
# 单侧同键重复 → 整键歧义，不配对
# ---------------------------------------------------------------------------


def test_ambiguous_key_not_paired():
    body = _audit(
        agreed=[
            _file("dup", H1),
            _file("DUP", H2),  # 约定侧同键重复（大小写折叠后同键）
        ],
        delivered=[_file("dup", H1)],
    )
    assert body["matched"] == []
    assert len(body["ambiguous_keys"]) == 1
    amb = body["ambiguous_keys"][0]
    assert amb["comparison_key"] == "dup"
    assert amb["canonical_path"] == "dup"
    assert _refs(amb["agreed"]) == [("agreed", 1), ("agreed", 2)]
    # 交付侧的同键单条也列入歧义键，绝不任选一个配对。
    assert _refs(amb["delivered"]) == [("delivered", 1)]
    assert body["missing"] == [] and body["extra"] == []
    assert body["summary"]["ambiguous_keys"] == 1
    assert body["summary"]["ambiguous_entries"] == 3


def test_ambiguous_when_both_sides_duplicated():
    body = _audit(
        agreed=[_dir("k"), _dir("k/")],
        delivered=[_dir("K"), _dir("K//")],
    )
    assert len(body["ambiguous_keys"]) == 1
    amb = body["ambiguous_keys"][0]
    assert _refs(amb["agreed"]) == [("agreed", 1), ("agreed", 2)]
    assert _refs(amb["delivered"]) == [("delivered", 1), ("delivered", 2)]


# ---------------------------------------------------------------------------
# 层级阻挡：另一侧文件占据祖先 → 非普通缺失/额外，并指明阻挡文件
# ---------------------------------------------------------------------------


def test_file_ancestor_blocks_descendant_instead_of_missing_extra():
    body = _audit(
        agreed=[
            _file("a/b/c.txt"),  # 仅约定有：交付的 a/b 是文件祖先 → 阻挡
            _file("a"),          # 仅约定有：它自身无另一侧文件祖先 → 缺失；
            #                    但作为祖先阻挡交付独有的 a/b 与 a/sub
        ],
        delivered=[
            _file("a/b"),        # 仅交付有：约定文件 a 是其祖先 → 阻挡（非额外）
            _dir("a/sub"),      # 仅交付有：约定文件 a 是其祖先（目录也被挡）
        ],
    )
    # 阻挡者自身在另一侧没有同键条目，也不被任何另一侧文件祖先阻挡 → 缺失。
    assert _refs(body["missing"]) == [("agreed", 2)]
    assert body["extra"] == []
    blocked = body["hierarchy_blocked"]
    assert [(h["entry"]["side"], h["entry"]["index"]) for h in blocked] == [
        ("delivered", 1),  # a/b
        ("agreed", 1),     # a/b/c.txt
        ("delivered", 2),  # a/sub
    ]
    del_ab, agr_abc, del_sub = blocked
    # a/b 被约定文件 a 阻挡。
    assert del_ab["entry"]["canonical_path"] == "a/b"
    assert (del_ab["blocked_by"]["side"], del_ab["blocked_by"]["index"]) == (
        "agreed",
        2,
    )
    # a/b/c.txt 被交付文件 a/b（最近祖先）阻挡。
    assert agr_abc["entry"]["canonical_path"] == "a/b/c.txt"
    assert agr_abc["blocked_by"] == {
        "side": "delivered",
        "index": 1,
        "type": "file",
        "original_path": "a/b",
        "canonical_path": "a/b",
        "comparison_key": "a/b",
        "sha256": H1,
    }
    assert agr_abc["additional_blocked_by"] == []
    assert del_sub["entry"]["canonical_path"] == "a/sub"
    assert (del_sub["blocked_by"]["side"], del_sub["blocked_by"]["index"]) == (
        "agreed",
        2,
    )
    assert body["summary"]["hierarchy_blocked"] == 3
    assert body["summary"]["missing"] == 1
    assert body["summary"]["extra"] == 0


def test_blocked_descendant_when_ancestor_pair_is_type_mismatch():
    # a/b 两侧都有但类型不符（约定目录 / 交付文件）；a/b/c 仅约定有，
    # 不能算普通缺失，而应归为交付文件 a/b 的层级阻挡。
    body = _audit(
        agreed=[_dir("a/b"), _file("a/b/c.txt")],
        delivered=[_file("a/b")],
    )
    assert _pairs(body["type_mismatches"]) == [("TYPE_MISMATCH", "a/b", 1, 1)]
    assert body["missing"] == []
    blocked = body["hierarchy_blocked"]
    assert len(blocked) == 1
    assert (blocked[0]["entry"]["side"], blocked[0]["entry"]["index"]) == (
        "agreed",
        2,
    )
    assert (
        blocked[0]["blocked_by"]["side"],
        blocked[0]["blocked_by"]["index"],
    ) == ("delivered", 1)


def test_nearest_ancestor_is_primary_blocker_with_additional():
    # 交付侧同批把 a 与 a/b 都列为文件（自身矛盾的清单也不 422）：
    # 约定独有的 a/b/c.txt 被阻挡，最近祖先 a/b 为首要阻挡，a 列入附加。
    body = _audit(
        agreed=[_file("a/b/c.txt")],
        delivered=[_file("a"), _file("a/b")],
    )
    blocked = body["hierarchy_blocked"]
    assert len(blocked) == 1
    h = blocked[0]
    assert (h["blocked_by"]["side"], h["blocked_by"]["index"]) == (
        "delivered",
        2,
    )
    assert _refs(h["additional_blocked_by"]) == [("delivered", 1)]


def test_directory_ancestor_does_not_block():
    # 另一侧祖先为目录属正常布局：独有后代按普通额外处理。
    body = _audit(
        agreed=[_dir("a"), _file("a/x.txt")],
        delivered=[_dir("a"), _file("a/x.txt"), _file("a/y.txt")],
    )
    assert _refs(body["extra"]) == [("delivered", 3)]
    assert body["hierarchy_blocked"] == []


# ---------------------------------------------------------------------------
# 稳定排序：与输入顺序无关
# ---------------------------------------------------------------------------


def test_output_stable_sorted_regardless_of_input_order():
    agreed = [_file("z.txt", H1), _file("m.txt", H1), _dir("d"), _file("a.txt", H1)]
    delivered = [
        _file("a.txt", H1),
        _dir("d"),
        _file("m.txt", H2),
        _file("z.txt", H2),
        _file("n.txt", H1),
    ]
    body1 = _audit(agreed, delivered)
    body2 = _audit(list(reversed(agreed)), list(reversed(delivered)))

    # 分类结果按比较键排序，与输入顺序无关。
    assert [p["comparison_key"] for p in body1["matched"]] == ["a.txt", "d"]
    assert [p["comparison_key"] for p in body2["matched"]] == ["a.txt", "d"]
    for key in ("type_mismatches", "digest_mismatches"):
        assert [p["comparison_key"] for p in body1[key]] == [
            p["comparison_key"] for p in body2[key]
        ]
    assert [p["comparison_key"] for p in body1["digest_mismatches"]] == [
        "m.txt",
        "z.txt",
    ]
    for key in ("missing", "extra", "hierarchy_blocked", "ambiguous_keys"):
        assert [e["comparison_key"] for e in body1[key]] == [
            e["comparison_key"] for e in body2[key]
        ]
    assert [e["comparison_key"] for e in body1["extra"]] == ["n.txt"]
    # 计数不受输入顺序影响。
    assert body1["summary"] == body2["summary"]

    # 但双方原下标随其在各自清单中的位置如实保留（不重排、不改写）。
    # 正序时 a.txt 是约定第 4 条；反转后是第 1 条。
    assert body1["matched"][0]["agreed"]["index"] == 4
    assert body2["matched"][0]["agreed"]["index"] == 1
    assert body1["matched"][0]["delivered"]["index"] == 1
    assert body2["matched"][0]["delivered"]["index"] == 5


def test_missing_and_blocked_sorted_by_key():
    body = _audit(
        agreed=[
            _file("z/deep/x.txt"),
            _file("a/deep/x.txt"),
            _file("m.txt"),
        ],
        delivered=[_file("z/deep"), _file("a/deep"), _file("z-extra.txt")],
    )
    assert [h["entry"]["comparison_key"] for h in body["hierarchy_blocked"]] == [
        "a/deep/x.txt",
        "z/deep/x.txt",
    ]
    assert [e["comparison_key"] for e in body["missing"]] == ["m.txt"]
    # 阻挡文件自身在约定侧没有同键条目、也不被约定文件祖先阻挡 → 计额外；
    # 按比较键稳定排序（"-" 0x2d 先于 "/" 0x2f）。
    assert [e["comparison_key"] for e in body["extra"]] == [
        "a/deep",
        "z-extra.txt",
        "z/deep",
    ]


# ---------------------------------------------------------------------------
# 422 校验：不安全路径 / SHA-256 要求
# ---------------------------------------------------------------------------


def _status(agreed, delivered):
    return client.post(
        "/api/v1/manifest-audit", json={"agreed": agreed, "delivered": delivered}
    ).status_code


def test_unsafe_paths_rejected_422():
    # 越界遍历（两侧分别校验，错误信息标注清单名）。
    assert _status([_file("../escape")], []) == 422
    assert _status([], [_file("../escape")]) == 422
    # 绝对路径、盘符、UNC、保留设备名、非法字符、空路径。
    assert _status([_file("/abs")], []) == 422
    assert _status([{"path": "C:\\x", "type": "file", "sha256": H1}], []) == 422
    assert _status([{"path": r"\\s\f", "type": "file", "sha256": H1}], []) == 422
    assert _status([_file("CON")], []) == 422
    assert _status([_file("a|b")], []) == 422
    assert _status([{"path": ".", "type": "directory"}], []) == 422


def test_file_requires_sha256_422():
    assert _status([{"path": "a.txt", "type": "file"}], []) == 422


def test_sha256_format_422():
    assert _status([_file("a.txt", "0" * 63)], []) == 422  # 长度不足
    assert _status([_file("a.txt", "z" * 64)], []) == 422  # 非十六进制
    assert _status([_file("a.txt", H1[:-1] + "G")], []) == 422


def test_directory_must_not_carry_sha256_422():
    assert _status([{"path": "d", "type": "directory", "sha256": H1}], []) == 422


def test_symlink_type_rejected_422():
    assert (
        client.post(
            "/api/v1/manifest-audit",
            json={
                "agreed": [{"path": "lnk", "type": "symlink"}],
                "delivered": [],
            },
        ).status_code
        == 422
    )


def test_only_declared_digests_compared():
    # 服务绝不读文件内容：传入任意自造摘要均可正常核对。
    body = _audit([_file("x.bin", H3)], [_file("x.bin", H3)])
    assert body["matched"][0]["delivered"]["sha256"] == H3
