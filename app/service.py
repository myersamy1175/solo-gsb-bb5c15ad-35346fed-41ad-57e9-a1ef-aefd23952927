"""预检编排：规范化 → selection_rules 筛选 → 符号链接风险分析 → 资源预算校验 → 同名碰撞分组 → 层级冲突检测（纯内存）。"""

from __future__ import annotations

from .normalize import Issue, IssueCode, IssueSeverity, NormalizedEntry, normalize_entry
from .mirror import MirrorOperation, plan_mirror
from .overlay import (
    OverlayNodeSource,
    OverlayStatus,
    apply_entry,
    build_existing_tree,
    build_manifest_tree,
)
from .repair import build_repair_plan
from .resource import DeclaredSizes, check_resource_budgets
from .schemas import (
    CollisionGroup,
    CollisionMember,
    EntryType,
    HierarchyConflict,
    MirrorPreviewRequest,
    MirrorPreviewResponse,
    MirrorStepOut,
    MirrorSummary,
    OverlayBlocker,
    OverlayEntryOut,
    OverlayEntryType,
    OverlayPreviewRequest,
    OverlayPreviewResponse,
    OverlaySummary,
    PreflightEntryOut,
    PreflightRepairResponse,
    PreflightRequest,
    PreflightResponse,
    Summary,
)
from .selection import apply_selection_rules
from .symlink import analyze_symlinks


def _add_issue(entry: NormalizedEntry, issue: Issue) -> None:
    # 避免重复追加同一相关下标的问题。
    if issue.code in (IssueCode.BLOCKED_BY_FILE_ANCESTOR, IssueCode.BLOCKS_DESCENDANT):
        for existing in entry.issues:
            if (
                existing.code == issue.code
                and existing.related_index == issue.related_index
            ):
                return
    entry.issues.append(issue)


def _collision_groups(entries: list[NormalizedEntry]) -> dict[int, int]:
    """为安全条目按 comparison_key 分组，返回 entry.index -> group 编号。

    符号链接不参与同名碰撞分组（其路径冲突由符号链接风险分析覆盖）。
    """
    buckets: dict[str, list[NormalizedEntry]] = {}
    first_seen: dict[str, int] = {}
    for entry in entries:
        if (
            entry.unsafe
            or entry.comparison_key is None
            or entry.entry_type == EntryType.SYMLINK.value
        ):
            continue
        key = entry.comparison_key
        if key not in buckets:
            buckets[key] = []
            first_seen[key] = entry.index
        buckets[key].append(entry)

    # 组号按该组首次出现的下标排序分配。
    ordered_keys = sorted(first_seen, key=lambda k: first_seen[k])
    index_to_group: dict[int, int] = {}
    group_of_key: dict[str, int] = {}
    for group_no, key in enumerate(ordered_keys, start=1):
        if len(buckets[key]) < 2:
            continue
        group_of_key[key] = group_no
        for entry in buckets[key]:
            index_to_group[entry.index] = group_no
    return index_to_group


def _hierarchy_conflicts(
    entries: list[NormalizedEntry],
) -> list[tuple[NormalizedEntry, NormalizedEntry]]:
    """文件占据祖先路径 → 阻挡同名前缀后代，返回 (文件条目, 后代条目) 对。

    仅对安全条目计算。键为小写折叠后的路径段，故不同大小写/点空格变体也会匹配。
    """
    files_by_key: dict[str, list[NormalizedEntry]] = {}
    for entry in entries:
        if entry.unsafe or entry.entry_type != EntryType.FILE.value:
            continue
        files_by_key.setdefault(entry.comparison_key, []).append(entry)

    conflicts: list[tuple[NormalizedEntry, NormalizedEntry]] = []
    for descendant in entries:
        if descendant.unsafe:
            continue
        parts = descendant.key_parts
        for depth in range(1, len(parts)):  # 不含自身（depth == len(parts)）
            ancestor_key = "/".join(parts[:depth])
            for ancestor in files_by_key.get(ancestor_key, ()):
                conflicts.append((ancestor, descendant))
    # 稳定输出：按后代下标、再按祖先下标排序。
    conflicts.sort(key=lambda pair: (pair[1].index, pair[0].index))
    return conflicts


def run_preflight(request: PreflightRequest) -> PreflightResponse:
    normalized: list[NormalizedEntry] = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.entries, start=1)
    ]

    # 选择规则在规范化之后、一切风险/冲突分析之前应用：被排除条目仍返回
    # 规范化信息，但不参与后续符号链接、资源预算、碰撞、层级冲突与 repair。
    rules = None
    if request.selection_rules is not None:
        rules = [(r.action.value, r.pattern) for r in request.selection_rules]
    decisions = apply_selection_rules(
        [n.canonical_parts for n in normalized], rules
    )
    selection = {e.index: d for e, d in zip(normalized, decisions)}
    active = [e for e in normalized if selection[e.index].selected]

    # 符号链接风险分析先于碰撞/层级检测：被劫持条目会被标记为不安全，
    # 从而退出后续碰撞分组、层级冲突与 repair 复检。
    # all_link_targets 仅用于逐条回显；被排除条目不进入风险分析。
    all_link_targets = {
        i: e.link_target
        for i, e in enumerate(request.entries, start=1)
        if e.link_target is not None
    }
    link_targets = {i: t for i, t in all_link_targets.items() if selection[i].selected}
    symlink_analysis = analyze_symlinks(active, link_targets)

    # 资源预算校验紧随符号链接分析：超限文件被标记为不安全，
    # 从而退出后续碰撞分组、层级冲突与 repair 复检。
    limits = request.resource_limits
    declared_sizes = {
        i: DeclaredSizes(
            compressed=e.compressed_bytes, uncompressed=e.uncompressed_bytes
        )
        for i, e in enumerate(request.entries, start=1)
        if (
            selection[i].selected
            and (e.compressed_bytes is not None or e.uncompressed_bytes is not None)
        )
    }
    resource_outcome = check_resource_budgets(
        active,
        declared_sizes,
        max_file_uncompressed_bytes=(
            limits.max_file_uncompressed_bytes if limits else None
        ),
        max_total_uncompressed_bytes=(
            limits.max_total_uncompressed_bytes if limits else None
        ),
        max_compression_ratio=limits.max_compression_ratio if limits else None,
    )

    index_to_group = _collision_groups(active)

    conflicts = _hierarchy_conflicts(active)
    blocked_by: dict[int, list[int]] = {}
    blocks: dict[int, list[int]] = {}
    hierarchy_out: list[HierarchyConflict] = []
    for ancestor, descendant in conflicts:
        blocked_by.setdefault(descendant.index, []).append(ancestor.index)
        blocks.setdefault(ancestor.index, []).append(descendant.index)
        _add_issue(
            ancestor,
            Issue(
                IssueCode.BLOCKS_DESCENDANT,
                IssueSeverity.INFO,
                f"文件占据祖先路径，会阻挡条目 #{descendant.index}（{descendant.canonical_path}）解包",
                related_index=descendant.index,
            ),
        )
        _add_issue(
            descendant,
            Issue(
                IssueCode.BLOCKED_BY_FILE_ANCESTOR,
                IssueSeverity.INFO,
                f"祖先路径已被条目 #{ancestor.index} 的文件（{ancestor.canonical_path}）占据，无法创建目录",
                related_index=ancestor.index,
            ),
        )
        hierarchy_out.append(
            HierarchyConflict(
                file_index=ancestor.index,
                file_canonical_path=ancestor.canonical_path,
                descendant_index=descendant.index,
                descendant_canonical_path=descendant.canonical_path,
            )
        )

    # 碰撞组明细（仅包含真实发生碰撞的组）。
    groups_detail: dict[int, dict] = {}
    for entry in active:
        group_no = index_to_group.get(entry.index)
        if group_no is None:
            continue
        detail = groups_detail.setdefault(
            group_no,
            {
                "group": group_no,
                "canonical_path": entry.canonical_path,
                "members": [],
            },
        )
        detail["members"].append(
            CollisionMember(index=entry.index, canonical_path=entry.canonical_path)
        )

    collision_groups = [
        CollisionGroup(**groups_detail[no])
        for no in sorted(groups_detail)
    ]
    group_members: dict[int, list[int]] = {
        no: [m.index for m in groups_detail[no]["members"]]
        for no in sorted(groups_detail)
    }

    # 碰撞提示问题（仅选中条目参与碰撞分组）。
    for entry in active:
        group_no = index_to_group.get(entry.index)
        if group_no is not None:
            _add_issue(
                entry,
                Issue(
                    IssueCode.COLLISION,
                    IssueSeverity.INFO,
                    f"规范化后与组 {group_no} 内的其他条目同名，解包时互相覆盖",
                ),
            )

    entry_out = [
        PreflightEntryOut(
            index=e.index,
            type=EntryType(e.entry_type),
            original_path=e.original_path,
            link_target=all_link_targets.get(e.index),
            canonical_path=e.canonical_path,
            comparison_key=e.comparison_key,
            safe=not e.unsafe,
            collision_group=index_to_group.get(e.index),
            blocked_by_file_ancestors=blocked_by.get(e.index, []),
            blocks_descendants=blocks.get(e.index, []),
            selected=selection[e.index].selected,
            matched_rule_index=selection[e.index].matched_rule_index,
            issues=[
                {
                    "code": i.code,
                    "severity": i.severity,
                    "message": i.message,
                    "segment": i.segment,
                    "related_index": i.related_index,
                }
                for i in e.issues
            ],
        )
        for e in normalized
    ]

    unsafe_count = sum(1 for e in normalized if e.unsafe)
    selected_count = sum(1 for d in decisions if d.selected)
    summary = Summary(
        total=len(normalized),
        safe=len(normalized) - unsafe_count,
        unsafe=unsafe_count,
        selected=selected_count,
        excluded=len(normalized) - selected_count,
        collisions=len(collision_groups),
        collision_entries=len(index_to_group),
        hierarchy_conflicts=len(hierarchy_out),
        symlinks=len(symlink_analysis.links),
        symlink_cycles=len(symlink_analysis.cycles),
        symlink_hijacked=len(symlink_analysis.hijacked_entries),
        declared_compressed_bytes=resource_outcome.declared_compressed_bytes,
        declared_uncompressed_bytes=resource_outcome.declared_uncompressed_bytes,
        resource_limits_exceeded=resource_outcome.exceeded,
    )

    if request.repair:
        repair_plan = build_repair_plan(
            entries=active,
            collision_groups=group_members,
            blocks=blocks,
            collisions_total=len(collision_groups),
            hierarchy_total=len(hierarchy_out),
        )
        return PreflightRepairResponse(
            target="windows",
            entries=entry_out,
            collision_groups=collision_groups,
            hierarchy_conflicts=hierarchy_out,
            symlink_analysis=symlink_analysis,
            summary=summary,
            repair_plan=repair_plan,
        )

    return PreflightResponse(
        target="windows",
        entries=entry_out,
        collision_groups=collision_groups,
        hierarchy_conflicts=hierarchy_out,
        symlink_analysis=symlink_analysis,
        summary=summary,
    )


def run_overlay_preview(request: OverlayPreviewRequest) -> OverlayPreviewResponse:
    """目标目录覆盖预演：现存清单建树后按序干跑解包清单（纯内存）。

    现存清单已在请求模型校验阶段通过
    :func:`app.overlay.validate_existing_entries` 检查（否则请求以 422
    被拒绝），此处直接构建虚拟目录树。
    """
    existing = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.existing, start=1)
    ]
    tree = build_existing_tree(existing)

    entries_out: list[OverlayEntryOut] = []
    counts = {status: 0 for status in OverlayStatus}
    for i, entry_in in enumerate(request.incoming, start=1):
        normalized = normalize_entry(
            index=i, entry_type=entry_in.type.value, original_path=entry_in.path
        )
        decision = apply_entry(tree, normalized)
        counts[decision.status] += 1

        blocker = None
        if decision.blocker is not None:
            blocker = OverlayBlocker(
                source=decision.blocker.source,
                index=decision.blocker.index,
                canonical_path=decision.blocker.canonical_path,
            )
        entries_out.append(
            OverlayEntryOut(
                index=i,
                type=OverlayEntryType(normalized.entry_type),
                original_path=normalized.original_path,
                canonical_path=normalized.canonical_path,
                comparison_key=normalized.comparison_key,
                safe=not normalized.unsafe,
                status=decision.status,
                blocked_reason=decision.reason,
                blocked_by=blocker,
                issues=[
                    {
                        "code": issue.code,
                        "severity": issue.severity,
                        "message": issue.message,
                        "segment": issue.segment,
                        "related_index": issue.related_index,
                    }
                    for issue in normalized.issues
                ],
            )
        )

    summary = OverlaySummary(
        total=len(entries_out),
        create=counts[OverlayStatus.CREATE],
        overwrite=counts[OverlayStatus.OVERWRITE],
        reuse_directory=counts[OverlayStatus.REUSE_DIRECTORY],
        blocked=counts[OverlayStatus.BLOCKED],
    )
    return OverlayPreviewResponse(
        target="windows", entries=entries_out, summary=summary
    )


def run_mirror_preview(request: MirrorPreviewRequest) -> MirrorPreviewResponse:
    """目录镜像同步预演：生成把现存布局同步为期望布局的操作序列（纯内存）。

    两侧清单已在请求模型校验阶段通过
    :func:`app.overlay.validate_existing_entries` 检查（否则请求以 422
    被拒绝），此处直接构建两棵虚拟目录树并交给
    :func:`app.mirror.plan_mirror` 排定操作。
    """
    existing = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.existing, start=1)
    ]
    incoming = [
        normalize_entry(index=i, entry_type=e.type.value, original_path=e.path)
        for i, e in enumerate(request.incoming, start=1)
    ]

    existing_tree = build_existing_tree(existing)
    desired_tree = build_manifest_tree(incoming, OverlayNodeSource.INCOMING)
    incoming_files = [
        (entry.index, entry.canonical_path, entry.comparison_key)
        for entry in incoming
        if entry.entry_type == "file"
    ]

    planned = plan_mirror(existing_tree, desired_tree, incoming_files)

    steps_out = [
        MirrorStepOut(
            order=step.order,
            operation=step.operation,
            node_type=OverlayEntryType(step.node_type),
            source=step.source,
            index=step.index,
            canonical_path=step.canonical_path,
            comparison_key=step.comparison_key,
        )
        for step in planned
    ]

    counts = {op: 0 for op in MirrorOperation}
    for step in planned:
        counts[step.operation] += 1
    summary = MirrorSummary(
        total=len(steps_out),
        delete=counts[MirrorOperation.DELETE],
        keep_directory=counts[MirrorOperation.KEEP_DIRECTORY],
        create_directory=counts[MirrorOperation.CREATE_DIRECTORY],
        create_file=counts[MirrorOperation.CREATE_FILE],
        overwrite_file=counts[MirrorOperation.OVERWRITE_FILE],
    )
    return MirrorPreviewResponse(
        target="windows", steps=steps_out, summary=summary
    )
