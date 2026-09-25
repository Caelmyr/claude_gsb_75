"""实时排行榜：增量更新 + 封榜（Scoreboard Freeze）。

存储（对应「成绩按竞赛/用户分片」）：
  data/scores/{contest_id}/{user_id}.json   每个用户一份成绩分片（权威数据）
  data/scores/{contest_id}/ranking.json     聚合榜单（增量维护、用于快速读取）

评分模式：
  - acm：按「解题数降序、罚时升序」排名；罚时 = 首次 AC 时刻(秒) + 错误次数 * 罚时
  - ioi：按「总分降序、总用时升序」排名；每题取历史最高分（部分分），
        再按竞赛为该题设置的分值折算：加权分 = 原始得分 / 题目满分 * 竞赛分值

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


def _contest_points_map(contest):
    """竞赛内每题分值映射：{problem_id: points}（缺省 100，不允许负数）。"""
    points = {}
    for item in contest.get("problems", []) or []:
        if not isinstance(item, dict):
            continue
        pid = item.get("problem_id")
        if not pid:
            continue
        try:
            points[pid] = max(0, int(item.get("points", 100)))
        except (TypeError, ValueError):
            points[pid] = 100
    return points


def _problem_full_points(problem_id):
    """题目原始满分：测试点分值之和，缺省时回退题目自身 points（默认 100）。

    与评测引擎的 full_points 口径保持一致，用于把原始得分折算到竞赛分值。
    """
    data = read_json(os.path.join(config.TESTCASES_DIR, f"{problem_id}.json"))
    total = 0
    for c in (data or {}).get("cases", []):
        try:
            total += int(c.get("points", 0))
        except (TypeError, ValueError):
            pass
    if total > 0:
        return total
    p = read_json(os.path.join(config.PROBLEMS_DIR, f"{problem_id}.json"))
    try:
        return int((p or {}).get("points", 100))
    except (TypeError, ValueError):
        return 100


def _clean_number(x):
    """整数值的浮点转为 int，保证榜单输出整洁。"""
    return int(x) if float(x).is_integer() else x


def _weighted_score(problem_id, raw_score, points_map, full_cache):
    """把某题原始得分按竞赛分值折算：raw / 题目满分 * 竞赛分值。

    未在竞赛中配置分值的题目按原始得分计入（权重取题目满分），
    保证历史行为不变；full_cache 避免重复读取题目/测试点文件。
    """
    if problem_id not in full_cache:
        full_cache[problem_id] = _problem_full_points(problem_id)
    full = full_cache[problem_id]
    if not full:
        return 0
    weight = (points_map or {}).get(problem_id, full)
    return _clean_number(round(raw_score * weight / full, 2))


def _summarize(record, mode, points_map=None, full_cache=None):
    """由用户成绩分片计算榜单摘要行。

    IOI 模式：每题得分按竞赛分值折算后汇总（榜单总分与竞赛设置一致）；
    ACM 模式：保持原始计分（每题 1/0），不受竞赛分值影响。
    """
    solved = 0
    score = 0
    penalty = 0
    total_time_ms = 0
    problems = {}
    for pid, p in record.get("problems", {}).items():
        if p.get("solved"):
            solved += 1
        penalty += p.get("penalty", 0)
        total_time_ms += p.get("time_ms", 0)
        raw = p.get("score", 0)
        if mode == "ioi":
            weighted = _weighted_score(pid, raw, points_map,
                                       full_cache if full_cache is not None else {})
            q = dict(p)
            q["score"] = weighted
            problems[pid] = q
            score += weighted
        else:
            problems[pid] = p
            score += raw
    if mode == "ioi":
        score = _clean_number(round(score, 2))
    return {
        "user_id": record["user_id"],
        "username": record.get("username", ""),
        "nickname": record.get("nickname", record.get("username", "")),
        "solved": solved,
        "score": score,
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
    """重建聚合榜单（扫描该竞赛全部分片并排序）。

    按竞赛当前配置（模式 + 每题分值）汇总，分值变更后重算即调用本函数。
    """
    contest_id = contest["id"]
    mode = contest.get("mode", "acm")
    points_map = _contest_points_map(contest)
    full_cache = {}
    d = _score_dir(contest_id)
    rows = []
    for name in list_files(d):
        if name == "ranking":
            continue
        rec = read_json(os.path.join(d, name + ".json"))
        if rec:
            rows.append(_summarize(rec, mode, points_map, full_cache))
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


def rebuild_contest_ranking(contest):
    """按当前竞赛配置重建聚合榜单并写回（分值/模式变更后榜单总分随之重算）。

    已冻结的封榜快照保持不变，解冻后展示重算结果。
    """
    ranking = _rebuild_ranking(contest)
    locked_update(_ranking_path(contest["id"]), lambda _d: ranking, default=ranking)
    return ranking


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

    result 由评测引擎给出，包含 status / score / time_ms / memory_kb 等。
    该函数在评测线程中调用，通过 storage 的文件级锁保证并发安全。
    """
    mode = contest.get("mode", "acm")
    penalty_seconds = int(config.DEFAULT_SETTINGS["ranking"]["penalty_seconds"])
    contest_id = contest["id"]
    user_id = user["id"]

    def _update(rec):
        if rec is None:
            rec = empty_user_record(
                contest_id, user_id, user.get("username", ""), user.get("nickname", "")
            )
        probs = rec.setdefault("problems", {})
        p = probs.setdefault(problem_id, {
            "solved": False, "attempts": 0, "first_solve_time": None,
            "score": 0, "time_ms": 0, "memory_kb": 0, "penalty": 0,
        })
        p["attempts"] += 1
        p["time_ms"] = max(p["time_ms"], result.get("time_ms", 0))
        p["memory_kb"] = max(p["memory_kb"], result.get("memory_kb", 0))

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
            p["score"] = max(p["score"], result.get("score", 0))
        elif mode == "acm":
            p["score"] = 1 if p["solved"] else 0
        return rec

    user_path = _user_path(contest_id, user_id)
    record = locked_update(user_path, _update, default=None)

    # 增量更新聚合榜单：重新扫描并排序（分数变化才触发）
    _maybe_freeze_snapshot(contest)
    rebuild_contest_ranking(contest)
    return record


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
