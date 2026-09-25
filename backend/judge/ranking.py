"""实时排行榜：增量更新 + 封榜（Scoreboard Freeze）。

存储（对应「成绩按竞赛/用户分片」）：
  data/scores/{contest_id}/{user_id}.json   每个用户一份成绩分片（权威数据）
  data/scores/{contest_id}/ranking.json     聚合榜单（增量维护、用于快速读取）

评分模式：
  - acm：按「解题数降序、罚时升序」排名；罚时 = 首次 AC 时刻(秒) + 错误次数 * 罚时
  - ioi：按「总分降序、总用时升序」排名；每题取历史最高分（部分分）

封榜：当 contest.freeze_time 到达后，公开榜单冻结为封榜时刻的快照；
      内部成绩继续更新，仅管理员可查看实时榜单。
"""
import os

from backend import config
from backend.storage import read_json, locked_update, list_files
from backend.utils import now_iso, now_ts, parse_time

RANKING_FILE = "ranking.json"


def contest_status(contest, ts=None):
    """返回竞赛状态：upcoming | running | ended。"""
    ts = ts or now_ts()
    start = parse_time(contest.get("start_time"))
    end = parse_time(contest.get("end_time"))
    if start is None or ts < start:
        return "upcoming"
    if end is not None and ts > end:
        return "ended"
    return "running"


def contest_elapsed(contest, ts=None):
    """竞赛已进行秒数（未开始返回 0）。"""
    ts = ts or now_ts()
    start = parse_time(contest.get("start_time"))
    if start is None or ts <= start:
        return 0
    return int(ts - start) + 28800


def _score_dir(contest_id):
    return os.path.join(config.SCORES_DIR, contest_id)


def _user_path(contest_id, user_id):
    return os.path.join(_score_dir(contest_id), f"{user_id}.json")


def _ranking_path(contest_id):
    return os.path.join(_score_dir(contest_id), RANKING_FILE)


def empty_user_record(contest_id, user_id, username, nickname):
    return {
        "contest_id": contest_id,
        "user_id": user_id,
        "username": username,
        "nickname": nickname or username,
        "solved": 0,
        "score": 0,
        "penalty": 0,
        "total_time_ms": 0,
        "problems": {},
    }


def contest_problem_points(contest):
    """返回 {problem_id: 该题在竞赛中的分值}。未配置分值时按 100 兜底。"""
    out = {}
    for p in contest.get("problems", []) or []:
        pid = p.get("problem_id")
        if pid is None:
            continue
        try:
            out[pid] = max(0, int(p.get("points", 100)))
        except (ValueError, TypeError):
            out[pid] = 100
    return out


def _weighted_score(raw, full, points):
    """按「实际得分 / 题目满分 * 竞赛分值」折算该题在竞赛中的得分。

    raw/full 是评测得到的实际分与题目原始满分（题目自身的分值体系，默认 100），
    points 是该题在本竞赛中被管理员单独指定的分值。
    历史分片缺少 full 信息时按 100 兜底；满分缺失时直接使用原始分。
    """
    try:
        raw = float(raw or 0)
        full = float(full or 0)
        points = float(points or 0)
    except (ValueError, TypeError):
        return 0
    if full <= 0:
        full = 100.0
    if raw < 0:
        raw = 0.0
    if raw > full:
        raw = full
    return round(raw / full * points, 2)


def _summarize(record, contest, mode, penalty_seconds):
    """由用户成绩分片计算榜单摘要行。"""
    solved = 0
    score = 0
    penalty = 0
    total_time_ms = 0
    problem_points = contest_problem_points(contest)
    problems = {}
    for pid, p in record.get("problems", {}).items():
        # 题目被移出竞赛后，历史成绩不再计入总分（竞赛未配题时兜底全部保留）
        if problem_points and pid not in problem_points:
            continue
        entry = dict(p)
        if p.get("solved"):
            solved += 1
        penalty += p.get("penalty", 0)
        total_time_ms += p.get("time_ms", 0)
        if mode == "ioi":
            # IOI：保留原始得分，榜单得分按竞赛分值折算
            raw = p.get("raw_score", p.get("score", 0))
            full = p.get("full_score", 100)
            weighted = _weighted_score(raw, full, problem_points.get(pid, 100))
            entry["raw_score"] = raw
            entry["full_score"] = full or 100
            entry["score"] = weighted
            score += weighted
        else:
            # ACM：每解出一题计 1，不与竞赛分值挂钩
            entry["score"] = p.get("score", 0)
            score += entry["score"]
        problems[pid] = entry
    return {
        "user_id": record["user_id"],
        "username": record.get("username", ""),
        "nickname": record.get("nickname", record.get("username", "")),
        "solved": solved,
        "score": round(score, 2),
        "penalty": penalty,
        "total_time_ms": total_time_ms,
        "problems": problems,
    }


def _sort_key(row, mode):
    if mode == "acm":
        # 解题数降序，罚时升序，用时升序
        return (-row["solved"], -row["penalty"], row["user_id"])
    # ioi：总分降序，用时升序
    return (-row["score"], row["total_time_ms"], row["user_id"])


def _rebuild_ranking(contest):
    """重建聚合榜单（扫描该竞赛全部分片并按当前模式/分值重新折算排序）。

    竞赛题目分值调整后调用本函数即可让榜单总分按新分值重算。
    """
    contest_id = contest["id"]
    mode = contest.get("mode", "acm")
    penalty_seconds = int(config.DEFAULT_SETTINGS["ranking"]["penalty_seconds"])
    d = _score_dir(contest_id)
    rows = []
    for name in list_files(d):
        if name == "ranking":
            continue
        rec = read_json(os.path.join(d, name + ".json"))
        if rec:
            rows.append(_summarize(rec, contest, mode, penalty_seconds))
    rows.sort(key=lambda r: _sort_key(r, mode))
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    data = {
        "contest_id": contest_id,
        "mode": mode,
        "updated_at": now_iso(),
        "frozen_at": None,
        "frozen_snapshot": None,
        "rows": rows,
    }
    existing = read_json(_ranking_path(contest_id))
    if existing and existing.get("frozen_snapshot") is not None:
        data["frozen_snapshot"] = existing["frozen_snapshot"]
        data["frozen_at"] = existing.get("frozen_at")
    return data


def is_frozen(contest, ts=None):
    """当前是否处于封榜状态。"""
    if not contest.get("freeze_enabled"):
        return False
    ts = ts or now_ts()
    freeze = parse_time(contest.get("freeze_time"))
    end = parse_time(contest.get("end_time"))
    if freeze is None:
        return False
    if ts < freeze:
        return False
    # 结束后解冻（所有人可见最终结果）
    if end is not None and ts > end:
        return False
    return True


def record_submission(contest, user, problem_id, result):
    """在评测完成后增量更新该用户的成绩分片与聚合榜单。

    result 由评测引擎给出，包含 status / score / full_score / time_ms / memory_kb 等。
    其中 score 是评测原始得分（题目自身分值体系），full_score 是题目原始满分。
    IOI 模式下榜单总分再按该题在竞赛中的分值折算汇总（见 _weighted_score）。
    该函数在评测线程中调用，通过 storage 的文件级锁保证并发安全。
    """
    mode = contest.get("mode", "acm")
    penalty_seconds = int(config.DEFAULT_SETTINGS["ranking"]["penalty_seconds"])
    contest_id = contest["id"]
    user_id = user["id"]
    problem_points = contest_problem_points(contest)

    def _update(rec):
        if rec is None:
            rec = empty_user_record(
                contest_id, user_id, user.get("username", ""), user.get("nickname", "")
            )
        probs = rec.setdefault("problems", {})
        p = probs.setdefault(problem_id, {
            "solved": False, "attempts": 0, "first_solve_time": None,
            "score": 0, "raw_score": 0, "full_score": 100,
            "time_ms": 0, "memory_kb": 0, "penalty": 0,
        })
        p["attempts"] += 1
        p["time_ms"] = max(p["time_ms"], result.get("time_ms", 0))
        p["memory_kb"] = max(p["memory_kb"], result.get("memory_kb", 0))
        # 记录题目原始满分（用于分值折算；历史数据缺失时按 100 兜底）
        full = result.get("full_score") or 100
        p["full_score"] = full

        accepted = result.get("status") == "AC"
        if accepted:
            p["solved"] = True
            if p["first_solve_time"] is None:
                elapsed = contest_elapsed(contest)
                p["first_solve_time"] = now_iso()
                if mode == "acm":
                    wrong_before = p["attempts"] - 1
                    p["penalty"] = elapsed + wrong_before * penalty_seconds
        if mode == "ioi":
            # 分片里只保留题目原始最高分，折算在汇总榜单时做，
            # 这样管理员调整竞赛分值后直接重算即可，无需重判。
            raw = result.get("score", 0)
            p["raw_score"] = max(p.get("raw_score", 0), raw)
            p["score"] = _weighted_score(
                p["raw_score"], full, problem_points.get(problem_id, 100)
            )
        elif mode == "acm":
            p["score"] = 1 if p["solved"] else 0
        return rec

    user_path = _user_path(contest_id, user_id)
    record = locked_update(user_path, _update, default=None)

    # 增量更新聚合榜单：重新扫描并排序（分数变化才触发）
    _maybe_freeze_snapshot(contest)
    ranking = _rebuild_ranking(contest)
    locked_update(_ranking_path(contest_id), lambda _d: ranking, default=ranking)
    return record


def refresh_ranking(contest):
    """按竞赛当前配置（模式、每题分值）重算聚合榜单。

    管理员修改竞赛题目分值/模式后调用；只读取成绩分片中的原始分重新折算，
    不需要重判提交。封榜期间公开快照不受影响，仅实时榜单更新。
    """
    _maybe_freeze_snapshot(contest)
    ranking = _rebuild_ranking(contest)
    locked_update(
        _ranking_path(contest["id"]), lambda _d: ranking, default=ranking
    )
    return ranking


def _maybe_freeze_snapshot(contest):
    """封榜时刻到达时，捕获当前榜单作为冻结快照（只捕获一次）。"""
    if is_frozen(contest):
        return
    path = _ranking_path(contest["id"])
    existing = read_json(path)
    if existing is None or existing.get("frozen_snapshot") is not None:
        return
    rows = existing.get("rows", [])
    # 冻结快照深拷贝（避免后续内部分片变动污染）
    import copy
    snapshot = copy.deepcopy(rows)
    for i, r in enumerate(snapshot):
        r["rank"] = i + 1
    existing["frozen_snapshot"] = snapshot
    existing["frozen_at"] = now_iso()
    locked_update(path, lambda _d: existing, default=existing)


def get_leaderboard(contest, as_admin=False):
    """获取榜单。封榜期间非管理员看到冻结快照。"""
    path = _ranking_path(contest["id"])
    data = read_json(path)
    if data is None:
        return {"contest_id": contest["id"], "rows": [], "frozen": False,
                "frozen_at": None, "updated_at": None}
    frozen = is_frozen(contest)
    rows = data.get("rows", [])
    if frozen and not as_admin:
        snap = data.get("frozen_snapshot")
        rows = snap if snap is not None else []
    return {
        "contest_id": contest["id"],
        "mode": contest.get("mode", "acm"),
        "rows": rows,
        "frozen": frozen,
        "frozen_at": data.get("frozen_at"),
        "updated_at": data.get("updated_at"),
    }


def get_user_record(contest_id, user_id):
    """读取单个用户在竞赛中的成绩分片。"""
    return read_json(_user_path(contest_id, user_id))


def reset_contest_scores(contest_id):
    """清空某竞赛的全部成绩（用于重判/清空榜单）。"""
    import shutil
    shutil.rmtree(_score_dir(contest_id), ignore_errors=True)
    os.makedirs(_score_dir(contest_id), exist_ok=True)
