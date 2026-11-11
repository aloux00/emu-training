"""技能考核考务：多站轮转排程（OSCE）+ 监考轮转 + 导出 + 钉钉推送。

排程模型
--------
时间以「格」为单位推进，一格 = tick_min 分钟（默认 10），各站时长必须是 tick 的整数倍。
- 长站（时长 > 一格，如 30 分钟的第二/三站）【整块分配】：一个考生连续占用一个考位
  [s, s+duration]，中途不拆分、不换考位、不插换场间隙；这是硬约束。
- 短站（时长 = 一格）随后紧贴长站填入空档（先往前挤，再往后接）；
- 每个考生同一时刻只出现在一个考位，同一考位同一时刻只容纳一人；
- 考生可只考部分考站，按实际所选站计算时长。

监考模型
--------
- rooms（考位数）与 teachers（该站需要的监考人数）相互独立：
  电脑考试站（如第二站）可配 rooms=2 / teachers=1，一名老师同时盯多名考生；
  人盯人站（如查体站）配 rooms=1 / teachers=1。
- 监考时段 = 该站全部考位占用时间的并集，按 duty_min 切块（0 = 整场不换人），
  每块派 teachers 名老师；同一老师同一时刻不会被重复派出。
"""
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlmodel import Session, select
from datetime import datetime
from io import BytesIO
import math
import random

import db
import models
import auth

router = APIRouter()


def _session():
    with Session(db.engine) as s:
        yield s


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# ======================= 排程算法 =======================

def _station_dict(st: models.ExamStation) -> dict:
    return {
        "id": st.id,
        "seq": st.seq, "name": st.name, "duration_min": st.duration_min,
        "rooms": max(1, st.rooms), "teachers": max(1, st.teachers),
        "location": st.location, "color": st.color,
        "enabled": bool(getattr(st, "enabled", True)),
        "elastic": bool(st.elastic), "auto_rooms": bool(st.auto_rooms),
        "align": bool(getattr(st, "align", False)),
        "teacher_ids": [int(x) for x in (getattr(st, "teacher_ids", "") or "").split(",") if x.strip().lstrip("-").isdigit()],
    }


def _cand_dict(c: models.ExamCandidate, all_seqs: list) -> dict:
    raw = (c.stations or "").strip()
    if raw:
        seqs = [int(x) for x in raw.split(",") if x.strip().isdigit()]
    else:
        seqs = list(all_seqs)
    return {
        "id": c.id, "student_id": c.student_id, "name": c.name,
        "group": c.group_name, "stations": seqs, "sort": c.sort,
    }


def _resolve_capacity(stations, cands, pool, tick, auto_scale=True, target_min=0):
    """按考生人数推导各站的考位数与监考人数（瓶颈自动扩容）。

    - 弹性站（elastic，电脑考试）：机位不设上限，固定 teachers 人（1 人可盯多台电脑）；
    - 其它站：k = ceil(该站总工作量 / 目标总时长)，考位数 = 监考人数 = k；
    - 目标总时长 T 的下界 = 单个考生走完全部所报站的时长（谁也不能比自己一条线更快）；
      若推出来的老师总数超过勾选人数，就逐步放宽 T，直到人数够用。
    返回 (stations, meta)。
    """
    dur = {s["seq"]: s["duration_min"] for s in stations}
    n, need = {}, {}
    for c in cands:
        for q in c.get("stations") or []:
            if q in dur:
                n[q] = n.get(q, 0) + 1
    for s in stations:
        need[s["seq"]] = n.get(s["seq"], 0) * s["duration_min"]
    floor_min = tick
    for c in cands:
        floor_min = max(floor_min, sum(dur.get(q, 0) for q in (c.get("stations") or []) if q in dur))

    if not auto_scale:
        out = []
        for s in stations:
            s2 = dict(s)
            if not s.get("elastic"):
                s2["teachers"] = max(1, s2.get("rooms", 1))   # 1 对 1：一个考位一位老师
            out.append(s2)
        return out, {"target_min": 0, "teacher_used": 0, "expanded": []}

    flex = {s["seq"] for s in stations if s.get("elastic")}
    T = max(target_min or 0, floor_min)
    guard = 0
    while True:
        out, used, expanded = [], 0, []
        for s in stations:
            s2 = dict(s)
            cnt = n.get(s["seq"], 0)
            if s["seq"] in flex:
                s2["rooms"] = max(1, cnt)
                s2["teachers"] = max(1, s.get("teachers", 1))
            else:
                k = max(1, int(math.ceil(need[s["seq"]] / float(T)))) if cnt else 1
                k = min(k, max(1, cnt))
                s2["rooms"] = k
                s2["teachers"] = k
                if k > max(1, s.get("rooms", 1)):
                    expanded.append({"seq": s["seq"], "rooms": k, "from": max(1, s.get("rooms", 1))})
            used += max(1, s2["teachers"])
            out.append(s2)
        if pool <= 0 or used <= pool or guard > 40:
            break
        T += tick
        guard += 1
    return out, {"target_min": T, "teacher_used": used, "expanded": expanded}


def _compact(rows, occ, tick, flex, align=None):
    """整段左移压缩：把每个考生的一整条时间带整体前移，直到任一考位被占。
    只移动不改顺序，因此不会产生新的空洞，也不会拆开 30 分钟的整块。
    含「对齐站」的考生不压缩：整段平移会把它从对齐好的时间块上拉走。"""
    align = align or set()
    for _ in range(6):
        moved = False
        order = sorted([r for r in rows if r["segs"]], key=lambda r: r["segs"][-1]["b"], reverse=True)
        for row in order:
            segs = row["segs"]
            if any(sg["seq"] in align for sg in segs):
                continue
            for sg in segs:                       # 先把自己从占用表里摘出来
                if sg["seq"] not in flex:
                    lst = occ[(sg["seq"], sg["room"])]
                    if (sg["a"], sg["b"]) in lst:
                        lst.remove((sg["a"], sg["b"]))
            shift = 0
            while segs[0]["a"] - (shift + tick) >= 0:
                d = shift + tick
                ok = True
                for sg in segs:
                    if sg["seq"] in flex:
                        continue
                    if any(sg["a"] - d < o[1] and o[0] < sg["b"] - d
                           for o in occ[(sg["seq"], sg["room"])]):
                        ok = False
                        break
                if not ok:
                    break
                shift = d
            if shift:
                for sg in segs:
                    sg["a"] -= shift
                    sg["b"] -= shift
                moved = True
            for sg in segs:
                if sg["seq"] not in flex:
                    occ[(sg["seq"], sg["room"])].append((sg["a"], sg["b"]))
        if not moved:
            break
    return rows


def _build_batch(stations, cands, tick, offset, rng):
    """单批排程，返回 (rows, makespan)。

    1. 固定考站：长站（30 分钟）整块排成骨架，短站（10 分钟）紧贴前后填入空档，中途不拆站；
    2. 弹性考站：机位不限、1 名老师可盯全部机位，排在最后填最大的空档，用来迁就其他考站；
    3. 同一考生同一时刻只在一个考站，同一考位同一时刻只容纳一人；
    4. 对齐站（align）：同站考生尽量落到同一时间块——刚性站吸附到「整点/半点」网格，
       弹性站则优先塞进同站已有的时间窗，让这一站的所有人集中在一段时间里考完。
    """
    dur = {s["seq"]: s["duration_min"] for s in stations}
    rooms = {s["seq"]: max(1, s["rooms"]) for s in stations}
    flex = {s["seq"] for s in stations if s.get("elastic")}
    align = {s["seq"] for s in stations if s.get("align")}
    flex_occ = {}     # 弹性站已排的时段，供对齐站判断「能不能和前面的人并到一起」
    flex_starts = {}  # 弹性站已用的开考时刻，对齐站优先复用同一列

    occ = {}
    for s in stations:
        if s["seq"] in flex:
            continue
        for r in range(rooms[s["seq"]]):
            occ[(s["seq"], r)] = []

    slot_total = max(1, sum(rooms[q] for q in rooms if q not in flex))
    total_need = sum(sum(dur[q] for q in c["stations"] if q in dur) for c in cands)
    limit = int(total_need / slot_total * 1.6) + 120

    def free_room(seq, a, b):
        if seq in flex:
            return 0
        for r in range(rooms[seq]):
            if all(not (a < o[1] and o[0] < b) for o in occ[(seq, r)]):
                return r
        return None

    def chain_free(seq, a, b):
        """短站成链用的空位检查：弹性站按「一列 1 人」算，保证三段轮转整齐。"""
        if seq in flex:
            if any(a < y and x < b for x, y in flex_occ.get(seq, [])):
                return None
            return 0
        return free_room(seq, a, b)

    # 并行短站组：时长为 1 个刻度的站（第一/四/五站）。它们互不占用同一考位，
    # 排成「同一列各站各一人」就能一批 3 人同时开考，所以选位时优先补齐人数不足的列。
    pset = [s["seq"] for s in stations if s["duration_min"] <= tick]
    # 一列能同时进几个人 = 各站考位数之和（弹性电脑考站按 1 人一批算，保持批次整齐）
    pcap = sum(1 if s["seq"] in flex else max(1, s["rooms"]) for s in stations
               if s["duration_min"] <= tick)

    def pcol(t, d):
        """t 这一列上，并行组里已经有多少名考生在考（同一考生不会同时在两站，直接数段即可）。"""
        n = 0
        for q2 in pset:
            if q2 in flex:
                n += sum(1 for a, b in flex_occ.get(q2, []) if a < t + d and t < b)
            else:
                for r in range(rooms[q2]):
                    n += sum(1 for a, b in occ[(q2, r)] if a < t + d and t < b)
        return n

    def same_at(seq, t, d, n=0):
        """这一站这一列是不是已经排满了。
        弹性站（1 名老师盯多台电脑）在「列还没满」时也尽量一人一批；
        列已满就允许叠加，否则它会变成全场的瓶颈。"""
        if seq in flex:
            if n >= pcap:
                return False
            return any(a < t + d and t < b for a, b in flex_occ.get(seq, []))
        cnt = sum(1 for r in range(rooms[seq]) for a, b in occ[(seq, r)] if a < t + d and t < b)
        return cnt >= rooms[seq]

    rows = []
    for cand in cands:
        seqs = [q for q in cand["stations"] if q in dur]
        hard = [q for q in seqs if q not in flex]
        flexs = [q for q in seqs if q in flex and dur[q] > tick]
        busy = []          # [(a, b, seq, room)]

        def stu_free(a, b):
            return all(not (a < x[1] and x[0] < b) for x in busy)

        def find_slot(seq, frm, step=0):
            d = dur[seq]
            if step > 1:                      # 对齐站：只从网格点（整点/半点）起考
                t = ((int(frm) + step - 1) // step) * step
            else:
                t = frm
            while t <= limit:
                if stu_free(t, t + d):
                    r = free_room(seq, t, t + d)
                    if r is not None:
                        return t, r
                t += (step if step > 1 else tick)
            return None

        longs = sorted([q for q in hard if dur[q] > tick], key=lambda q: -dur[q])
        if len(longs) > 1:
            # 对齐站先落地当骨架（它要在整点上起考），其余长站再随机换序试不同方案
            head = [q for q in longs if q in align]
            tail = [q for q in longs if q not in align]
            rng.shuffle(tail)
            longs = head + tail

        ok = True
        cursor = 0
        for q in longs:
            step = dur[q] if q in align else 0
            at = find_slot(q, cursor, step)
            if not at and cursor > 0:
                at = find_slot(q, 0, step)
            if not at and step:               # 网格上排不下就退回逐刻度，「尽量」对齐而不牺牲可排性
                at = find_slot(q, cursor)
                if not at and cursor > 0:
                    at = find_slot(q, 0)
            if not at:
                ok = False
                break
            s, r = at
            busy.append((s, s + dur[q], q, r))
            occ[(q, r)].append((s, s + dur[q]))
            cursor = max(cursor, s + dur[q])
        if not ok:
            rows.append({"cand": cand, "segs": None})
            continue

        first = min([x[0] for x in busy], default=0)
        last = max([x[1] for x in busy], default=0)

        # ---- 短站成链：同一考生的第一/四/五站排成连续的一段 ----
        # 三段都是 1 个刻度，连着走 → 考生不用在两站之间干等；相邻考生错开一格起考，
        # 每格正好「一站一人」，三人一批同时考，形成轮转（也顺手满足「三人一批」）。
        chain = sorted([q for q in seqs if dur[q] <= tick], key=lambda q: (q in flex, q))

        def chain_plan(seqlist):
            """给这一组短站找一个整体可行的连续时段，返回 [(a,b,seq,room)] 或 None。"""
            k = len(seqlist)
            bestp = None
            for t0 in range(0, limit + 1, tick):
                plan, used, okc = [], set(), True
                for m in range(k):
                    a, b = t0 + m * tick, t0 + (m + 1) * tick
                    if not stu_free(a, b):
                        okc = False
                        break
                    pick = None
                    for q in seqlist:
                        if q in used:
                            continue
                        r = chain_free(q, a, b)
                        if r is not None:
                            pick = (q, r)
                            break
                    if pick is None:
                        okc = False
                        break
                    used.add(pick[0])
                    plan.append((a, b, pick[0], pick[1]))
                if not okc:
                    continue
                # 先让考生自己少等待（空洞、跨度），再看这一段有没有顺着别人的时间走
                segs = sorted(list(busy) + plan)
                span = segs[-1][1] - segs[0][0]
                hole, prev = 0, segs[0][0]
                for a, b, _q, _r in segs:
                    hole += max(0, a - prev)
                    prev = max(prev, b)
                packed = sum(1 for m in range(k) if pcol(t0 + m * tick, tick) > 0)
                sc = (hole, span, -packed, t0)
                if bestp is None or sc < bestp[0]:
                    bestp = (sc, plan)
            return bestp[1] if bestp else None

        shorts = [q for q in chain if q not in flex]   # 成链失败时的兜底：仍然逐站填缝
        chained = False
        if chain:
            plan = chain_plan(chain)
            if plan:
                chained = True
                shorts = []
                for a, b, q, r in plan:
                    busy.append((a, b, q, r))
                    if q in flex:
                        flex_occ.setdefault(q, []).append((a, b))
                        if a not in flex_starts.setdefault(q, []):
                            flex_starts[q].append(a)
                    else:
                        occ[(q, r)].append((a, b))
                first = min(first, plan[0][0])
                last = max(last, plan[-1][1])
        if not chained:
            # 成链失败：弹性短站退回逐站填缝，交给下面的 flexs 循环
            flexs = flexs + [q for q in chain if q in flex]

        for q in shorts:
            d = dur[q]
            cands = []
            t = first
            while t + d <= last:                 # 1) 先补内部空洞，保证整段连续
                if stu_free(t, t + d):
                    r = free_room(q, t, t + d)
                    if r is not None:
                        cands.append((0, t, r))
                t += tick
            if stu_free(last, last + d):         # 2) 紧接在末尾
                r = free_room(q, last, last + d)
                if r is not None:
                    cands.append((1, last, r))
            t = first - d
            while t >= 0:                        # 3) 再往前贴着放
                if stu_free(t, t + d):
                    r = free_room(q, t, t + d)
                    if r is not None:
                        cands.append((2, t, r))
                t -= tick
            at = None
            if cands:
                # 同类别里优先「这一列还没满」且人数越接近满越好的位置：
                # 第一/四/五站因此排成同一列各一人，十分钟一批、三人同时开考
                scored = []
                for kind, t, r in cands:
                    n = pcol(t, d)
                    scored.append(((kind, 0 if n < pcap else 1,
                                    1 if same_at(q, t, d, n) else 0,
                                    -n, abs(t - first)), t, r))
                scored.sort(key=lambda x: x[0])
                at = (scored[0][1], scored[0][2])
            if not at:
                at = find_slot(q, last)
            if not at:
                ok = False
                break
            s, r = at
            busy.append((s, s + d, q, r))
            occ[(q, r)].append((s, s + d))
            first = min(first, s)
            last = max(last, s + d)
        if not ok:
            rows.append({"cand": cand, "segs": None})
            continue

        for q in flexs:
            d = dur[q]
            if not busy:
                s = 0
            else:
                best = None
                t = 0
                while t <= limit:
                    if stu_free(t, t + d):
                        seg = sorted(busy + [(t, t + d, q, 0)])
                        span = seg[-1][1] - seg[0][0]
                        hole = 0
                        prev = seg[0][0]
                        for a, b, _, _2 in seg:
                            hole += max(0, a - prev)
                            prev = max(prev, b)
                        if q in pset:
                            # 短站（含弹性电脑考站）：优先补齐人少的列，凑成「三人一批」
                            n = pcol(t, d)
                            score = (0 if n < pcap else 1, 1 if same_at(q, t, d, n) else 0,
                                     -n, span, hole)
                        elif q in align:
                            # 对齐站：优先与同站已排的考生「同一时刻开考」（机位不限，人越集中老师越省），
                            # 其次是整点/半点起考，最后才看考生自己的时间跨度
                            starts = flex_starts.setdefault(q, [])
                            ov = sum(max(0, min(t + d, y) - max(t, x)) for x, y in flex_occ.get(q, []))
                            if starts and t in starts:
                                lv = 0
                            elif t % d == 0:
                                lv = 1
                            elif ov > 0:
                                lv = 2
                            else:
                                lv = 3
                            # 贴住考生自己已有的一段（少干等）与「和别人同列」同等重要：
                            # 两者冲突时用 hole 决胜，宁可多一列也不让考生白等半小时
                            score = (lv + (0 if hole == 0 else 1), hole, -ov, span)
                        else:
                            score = (0, 0, span, hole)
                        if best is None or score < best[0]:
                            best = (score, t)
                    t += tick
                if best is None:
                    at = find_slot(q, last)
                    if not at:
                        ok = False
                        break
                    s = at[0]
                else:
                    s = best[1]
            busy.append((s, s + d, q, 0))
            flex_occ.setdefault(q, []).append((s, s + d))
            if s not in flex_starts.setdefault(q, []):
                flex_starts[q].append(s)
            first = min(first, s)
            last = max(last, s + d)
        if not ok:
            rows.append({"cand": cand, "segs": None})
            continue

        segs = sorted([{"seq": x[2], "room": x[3], "a": x[0], "b": x[1]}
                       for x in busy], key=lambda x: x["a"])
        rows.append({"cand": cand, "segs": segs})

    _compact(rows, occ, tick, flex, align)
    makespan = max([row["segs"][-1]["b"] for row in rows if row["segs"]], default=0)
    return rows, makespan


def build_schedule(stations, candidates, tick=10, duty_min=0, split_size=0,
                   teachers=None, exam_date="", seed_runs=8, auto_scale=True, target_min=0,
                   gap=10, expand_penalty=20, align_penalty=12):
    """返回 {slots, duties, ticks, batches, makespan, stations, capacity}。

    gap            = 场间换场分钟数（分场时生效）。
    expand_penalty = 每多派 1 名监考老师折算的分钟数（默认 20）：扩容只有在
                     「省下的时长 ÷ 新增老师数 > expand_penalty」时才划算，否则用老师最少的方案。
    align_penalty  = 对齐站每多出一段「各自为政」的时间/一次未对齐起考，折算的分钟数（默认 12）。
    """
    stations = [dict(s) for s in stations if s.get("enabled", True) and s["duration_min"] > 0]
    if not stations:
        return {"slots": [], "duties": [], "ticks": 0, "batches": 0, "makespan": 0,
                "stations": [], "capacity": {}}
    for s in stations:
        s["duration_min"] = max(tick, int(round(s["duration_min"] / tick)) * tick)
    dur_of = {s["seq"]: s["duration_min"] for s in stations}
    align_seqs = {s["seq"] for s in stations if s.get("align")}
    flex_seqs = {s["seq"] for s in stations if s.get("elastic")}

    cands = [c for c in candidates if c.get("stations")]
    pool_n = len(teachers or [])
    runs = max(1, min(16, seed_runs))

    # 两套容量方案各排一遍，再按「时长 + 老师成本」比价，谁划算用谁：
    #   min  —— 老师最少：各站按设定的考位数，1 考位 1 老师，不扩容；
    #   auto —— 瓶颈自动扩容：按目标时长推导考位数，可能加考位、加老师。
    plans = [("min", False)]
    if auto_scale:
        plans.insert(0, ("auto", True))

    best, by_plan = None, {}
    for name, flag in plans:
        for k in range(runs):
            rng = random.Random(k * 7919 + 13)
            if split_size and split_size > 0 and len(cands) > split_size:
                groups = [cands[i:i + split_size] for i in range(0, len(cands), split_size)]
            else:
                grp0 = list(cands)
                rng.shuffle(grp0)
                groups = [grp0]

            slots, offset, total_span = [], 0, 0
            used_sts, used_meta = None, None
            for bi, grp in enumerate(groups):
                sts, meta = _resolve_capacity(stations, grp, pool_n, tick, flag, target_min)
                if used_sts is None:
                    used_sts, used_meta = sts, meta
                rows, span = _build_batch(sts, grp, tick, offset, rng)
                for row in rows:
                    if not row["segs"]:
                        continue
                    for sg in row["segs"]:
                        slots.append({
                            "station_seq": sg["seq"], "room": sg["room"],
                            "start_min": sg["a"] + offset, "end_min": sg["b"] + offset,
                            "candidate_id": row["cand"]["id"], "batch": bi,
                            # 带上 student_id：保存考生后 ExamCandidate.id 可能重建，
                            # 前端按 student_id 对齐才不会错位
                            "student_id": row["cand"].get("student_id"),
                        })
                offset += span + (max(0, int(gap)) if bi < len(groups) - 1 else 0)
                total_span = offset

            ticks = max([int(round(s["end_min"] / tick)) for s in slots], default=0)
            miss = len(cands) - len({s["candidate_id"] for s in slots})
            by_c = {}
            for it in slots:
                by_c.setdefault(it["candidate_id"], []).append(it)
            holes = 0
            for lst in by_c.values():
                lst.sort(key=lambda x: x["start_min"])
                holes += sum(1 for a, b in zip(lst, lst[1:]) if b["start_min"] > a["end_min"])
            teacher_n = sum(max(1, s.get("teachers", 1)) for s in (used_sts or []))
            # 对齐站：时间越集中越好。弹性站看「被拆成几段时间」，刚性站看「有几个人没在整点起考」
            align_miss = 0
            for q in (align_seqs & {x["station_seq"] for x in slots}):
                ss = [x for x in slots if x["station_seq"] == q]
                if q in flex_seqs:
                    iv = sorted((x["start_min"], x["end_min"]) for x in ss)
                    win, end = 1, iv[0][1]
                    for a, b in iv[1:]:
                        if a >= end:
                            win += 1
                        end = max(end, b)
                    align_miss += win - 1
                else:
                    align_miss += sum(1 for x in ss if x["start_min"] % dur_of[q])
            # 老师成本按 expand_penalty 分钟/人折算进时长，避免「多派老师却一分钟没省」；
            # 空洞只作轻量惩罚（2 分钟/次），否则会为了少排队而把整天拉长
            score = (miss * 1000000 + total_span + expand_penalty * teacher_n
                     + 2 * holes + align_penalty * align_miss)
            item = {"slots": slots, "ticks": ticks, "batches": len(groups),
                    "makespan": total_span, "_score": score,
                    "stations": used_sts or [], "capacity": used_meta or {},
                    "_teachers": teacher_n, "_plan": name}
            if name not in by_plan or score < by_plan[name]["_score"]:
                by_plan[name] = item
            if best is None or score < best["_score"]:
                best = item

    winner = best["_plan"]
    cap = dict(best["capacity"] or {})
    cap["variant"] = winner
    cap["teachers"] = best["_teachers"]
    cap["makespan"] = best["makespan"]
    cap["expand_penalty"] = expand_penalty
    # 另一套方案的结果，供前端显示「多花几个老师能省多少分钟」的取舍提示
    others = [v for k, v in by_plan.items() if k != winner]
    if others:
        alt = min(others, key=lambda x: x["_score"])
        cap["alt"] = {"variant": alt["_plan"], "teachers": alt["_teachers"],
                      "makespan": alt["makespan"],
                      "expanded": (alt["capacity"] or {}).get("expanded", [])}
    if winner != "auto":
        cap["expanded"] = []          # 选了老师最少的方案，就不该显示扩容提示

    duties = _assign_duty(best["stations"], best["slots"], duty_min, teachers or [], tick)
    best.pop("_score", None)
    best.pop("_teachers", None)
    best.pop("_plan", None)
    best["capacity"] = cap
    best["duties"] = duties
    return best


def _st_tids(st):
    """考站上人工指定的考官 id 列表（兼容 dict 与 SQLModel）。"""
    v = st.get("teacher_ids", "") if isinstance(st, dict) else getattr(st, "teacher_ids", "")
    if isinstance(v, (list, tuple)):
        return [int(x) for x in v if str(x).isdigit()]
    return [int(x) for x in (v or "").split(",") if x.strip().isdigit()]


def _assign_duty(stations, slots, duty_min, teachers, tick):
    """按「站 + 连续占用段」派监考（时段 = 该站全部考位占用的并集）。

    - teachers < rooms 时一名老师同时盯多个考位（电脑考试站）；duty_min=0 整场不换人；
    - 派单顺序 = 历史负担轻的优先：load 大的（近期监考多）排在后面，等价于「推荐权重下降」；
    - 考站若人工指定了考官（teacher_ids），该站优先派这些人，冲突时才回退到全体。
    """
    if not teachers:
        return []
    load = {t["id"]: float(t.get("load") or 0) for t in teachers}   # 历史折算分钟
    busy = {t["id"]: [] for t in teachers}

    def pick(a, b, prefer=None):
        cand = [t for t in teachers if t["id"] in prefer] if prefer else list(teachers)
        if not cand:
            cand = list(teachers)
        for t in sorted(cand, key=lambda x: (load[x["id"]], x["id"])):
            if all(not (a < o[1] and o[0] < b) for o in busy[t["id"]]):
                busy[t["id"]].append((a, b))
                load[t["id"]] += (b - a)
                return t
        return None

    out = []
    for st in stations:
        spans = sorted([(s["start_min"], s["end_min"]) for s in slots
                        if s["station_seq"] == st["seq"]])
        blocks = []
        for a, b in spans:                      # 合并为该站的监考覆盖时段（小空档一并覆盖，避免一人一站被切成两段）
            if blocks and a - blocks[-1][1] <= 2 * tick:
                blocks[-1][1] = max(blocks[-1][1], b)
            else:
                blocks.append([a, b])
        for a, b in blocks:
            step = duty_min if duty_min and duty_min > 0 else (b - a)
            t = a
            while t < b:
                e = min(b, t + step)
                need = max(1, st["teachers"])
                prefer = _st_tids(st)
                got = []
                for _ in range(need):
                    p = pick(t, e, prefer)
                    if p is None and prefer:
                        p = pick(t, e, None)      # 指定的人都被占用 → 回退全体
                    if p:
                        got.append(p)
                if got:
                    out.append({
                        "station_seq": st["seq"], "room": -1,   # -1 = 该站全部考位
                        "start_min": t, "end_min": e,
                        "teacher_id": got[0]["id"], "teacher_name": got[0]["name"],
                        "teachers": [x["name"] for x in got],
                        "teacher_ids": [x["id"] for x in got],
                        "short": len(got) < need,
                    })
                t = e
    return out


# ======================= 接口：计划 =======================

def _default_stations(plan_id):
    """默认 5 站。

    考站性质（决定排程与监考模型）：
    - 第一站 10 分钟、第二站 30 分钟：电脑考试 → 弹性站，机位不设上限，1 名老师可盯多名考生，
      排程时迁就其它考站填空档；
    - 第三站 30 分钟、第四/五站 10 分钟：师生 1 对 1 对话 → 固定站，人盯人，
      考位数 = 监考人数，人数多时按工作量自动扩容。
    """
    cfg = [(1, "第一站", 10, 1, 1, True, "#f7d774"), (2, "第二站", 30, 1, 1, True, "#8ed4a6"),
           (3, "第三站", 30, 2, 2, False, "#9cc6ee"), (4, "第四站", 10, 1, 1, False, "#f2a2a2"),
           (5, "第五站", 10, 1, 1, False, "#c3aee0")]
    return [models.ExamStation(plan_id=plan_id, seq=q, name=n, duration_min=d,
                               rooms=r, teachers=t, location="", elastic=e, color=c,
                               align=(q == 2))
            for q, n, d, r, t, e, c in cfg]


@router.get("/plans")
def list_plans(s: Session = Depends(_session), _=Depends(auth.staff_only)):
    rows = s.exec(select(models.ExamPlan).order_by(models.ExamPlan.exam_date.desc())).all()
    out = []
    for p in rows:
        cnt = len(s.exec(select(models.ExamCandidate).where(
            models.ExamCandidate.plan_id == p.id)).all())
        out.append({"id": p.id, "name": p.name, "exam_date": p.exam_date,
                    "start_time": p.start_time, "status": p.status,
                    "candidates": cnt})
    return out


@router.post("/plans")
def create_plan(payload: dict, s: Session = Depends(_session), _=Depends(auth.staff_only)):
    p = models.ExamPlan(
        name=payload.get("name") or "技能考核",
        exam_date=payload.get("exam_date") or datetime.now().strftime("%Y-%m-%d"),
        start_time=payload.get("start_time") or "12:30",
        tick_min=int(payload.get("tick_min") or 10),
        gap_min=int(payload.get("gap_min") or 1),
        duty_min=int(payload.get("duty_min") or 0),
        split_size=int(payload.get("split_size") or 0),
        auto_scale=bool(payload.get("auto_scale", True)),
        created_at=_now(),
    )
    s.add(p)
    s.commit()
    s.refresh(p)
    for st in _default_stations(p.id):
        s.add(st)
    s.commit()
    return {"id": p.id}


@router.delete("/plans/{pid}")
def delete_plan(pid: int, s: Session = Depends(_session), _=Depends(auth.admin_only)):
    p = s.get(models.ExamPlan, pid)
    if p:
        s.delete(p)
    for m in (models.ExamStation, models.ExamCandidate, models.ExamSlot, models.ExamDuty):
        for row in s.exec(select(m).where(m.plan_id == pid)).all():
            s.delete(row)
    s.commit()
    return {"ok": True}


@router.get("/plans/{pid}")
def get_plan(pid: int, s: Session = Depends(_session), _=Depends(auth.staff_only)):
    p = s.get(models.ExamPlan, pid)
    if not p:
        raise HTTPException(404, "考核计划不存在")
    sts = s.exec(select(models.ExamStation).where(
        models.ExamStation.plan_id == pid).order_by(models.ExamStation.seq)).all()
    cands = s.exec(select(models.ExamCandidate).where(
        models.ExamCandidate.plan_id == pid).order_by(models.ExamCandidate.sort)).all()
    slots = s.exec(select(models.ExamSlot).where(models.ExamSlot.plan_id == pid)).all()
    duties = s.exec(select(models.ExamDuty).where(models.ExamDuty.plan_id == pid)).all()
    return {
        "plan": p.dict(),
        # 用 _station_dict 输出：teacher_ids 统一为 id 数组（st.dict() 会是逗号字符串）
        "stations": [_station_dict(st) for st in sts],
        "candidates": [c.dict() for c in cands],
        "slots": [x.dict() for x in slots],
        "duties": [x.dict() for x in duties],
    }


@router.get("/plans/{pid}/export")
def export_xlsx(pid: int, s: Session = Depends(_session), _=Depends(auth.staff_only)):
    """导出 xlsx：标题「YYYY年MM月技能出科考核」，保留考站底色，并附监考老师与监考时长。

    工作表 1 = 考生 × 时间网格（同站连续格合并、按考站颜色填底）；
    工作表 2 = 监考明细（考站/时段/老师/时长/地点）+ 老师监考时长汇总。
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.properties import PageSetupProperties
    from fastapi.responses import StreamingResponse
    from urllib.parse import quote

    p = s.get(models.ExamPlan, pid)
    if not p:
        raise HTTPException(404, "考核计划不存在")
    sts = s.exec(select(models.ExamStation).where(
        models.ExamStation.plan_id == pid).order_by(models.ExamStation.seq)).all()
    slots = s.exec(select(models.ExamSlot).where(models.ExamSlot.plan_id == pid)).all()
    if not slots:
        raise HTTPException(400, "还没有排程结果，请先生成并保存草稿")
    cands = s.exec(select(models.ExamCandidate).where(
        models.ExamCandidate.plan_id == pid).order_by(models.ExamCandidate.sort)).all()
    duties = s.exec(select(models.ExamDuty).where(models.ExamDuty.plan_id == pid)).all()
    tinfo = {t.id: t for t in s.exec(select(models.Teacher)).all()}
    tnames = {k: v.name for k, v in tinfo.items()}
    st_map = {st.seq: st for st in sts}

    tick = max(1, int(p.tick_min or 10))
    ticks = max(1, max(int(math.ceil(x.end_min / tick)) for x in slots))
    makespan = max(x.end_min for x in slots)
    try:
        d0 = datetime.strptime(p.exam_date, "%Y-%m-%d")
    except Exception:
        d0 = datetime.now()
    title = "%d年%d月技能出科考核" % (d0.year, d0.month)

    THIN = Side(style="thin", color="CBD5E1")
    BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
    HEAD_FILL = PatternFill("solid", fgColor="EEF2F7")
    HEAD_FONT = Font(size=10, bold=True, color="334155")
    CENTER = Alignment(horizontal="center", vertical="center", wrap_text=False)

    def fill_of(color, default="FFFFFF"):
        c = (color or "").strip().lstrip("#")
        if len(c) == 6 and all(ch in "0123456789abcdefABCDEF" for ch in c):
            return PatternFill("solid", fgColor="FF" + c.upper())
        return PatternFill("solid", fgColor="FF" + default)

    def head_row(ws, row, values, start=1):
        for i, v in enumerate(values):
            c = ws.cell(row=row, column=start + i, value=v)
            c.font = HEAD_FONT
            c.fill = HEAD_FILL
            c.border = BORDER
            c.alignment = CENTER
        ws.row_dimensions[row].height = 22

    def big_title(ws, text, last_col):
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
        c = ws.cell(row=1, column=1, value=text)
        c.font = Font(size=17, bold=True, color="1F2937")
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[1].height = 32

    def sub_line(ws, text, last_col):
        ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=last_col)
        c = ws.cell(row=2, column=1, value=text)
        c.font = Font(size=10, color="64748B")
        c.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[2].height = 20

    wb = Workbook()
    ws = wb.active
    ws.title = "考务安排"

    rows = [c for c in cands if getattr(c, "is_out", True)]
    by_cand = {}
    for x in slots:
        by_cand.setdefault(x.candidate_id, []).append(x)
    if not rows:                        # 只有排程没有考生档案时，按 candidate_id 兜底出行
        from types import SimpleNamespace
        rows = [SimpleNamespace(id=k, name="考生%d" % (i + 1), group_name="")
                for i, k in enumerate(sorted(by_cand))]

    last_col = 2 + ticks + 1
    big_title(ws, title, last_col)
    sub_line(ws, "%s  %s 开考 ｜ 考生 %d 人 ｜ %d 站 ｜ 总时长 %d 分钟" % (
        p.exam_date or "", p.start_time or "", len(rows), len(sts), makespan), last_col)

    head_row(ws, 3, ["考生", "组别"] + [_hhmm(p.start_time, i * tick) for i in range(ticks)] + ["已考站"])

    for idx, cand in enumerate(rows):
        r = 4 + idx
        ws.row_dimensions[r].height = 21
        c1 = ws.cell(row=r, column=1, value=cand.name)
        c1.font = Font(size=10, bold=True)
        c1.border = BORDER
        c1.alignment = Alignment(horizontal="left", vertical="center")
        c2 = ws.cell(row=r, column=2, value=cand.group_name or "")
        c2.font = Font(size=10, color="64748B")
        c2.border = BORDER
        c2.alignment = CENTER
        segs = sorted(by_cand.get(cand.id, []), key=lambda y: y.start_min)
        for sg in segs:
            st = st_map.get(sg.station_seq)
            a = max(0, min(ticks - 1, int(round(sg.start_min / tick))))
            b = max(a + 1, min(ticks, int(round(sg.end_min / tick))))
            col1, col2 = 3 + a, 3 + b - 1
            label = (st.name if st else "第%d站" % sg.station_seq)
            if st and not st.elastic and (st.rooms or 1) > 1:
                label += chr(65 + max(0, sg.room or 0))
            if col2 > col1:
                ws.merge_cells(start_row=r, start_column=col1, end_row=r, end_column=col2)
            cell = ws.cell(row=r, column=col1, value=label)
            cell.font = Font(size=9, bold=False, color="1F2937")
            cell.alignment = Alignment(horizontal="center", vertical="center", shrink_to_fit=True)
            f = fill_of(st.color if st else "")
            for cc in range(col1, col2 + 1):
                t = ws.cell(row=r, column=cc)
                t.fill = f
                t.border = BORDER
        done = "→".join((st_map[x.station_seq].name if x.station_seq in st_map else "第%d站" % x.station_seq)
                        for x in segs)
        cd = ws.cell(row=r, column=last_col, value=done)
        cd.font = Font(size=9, color="475569")
        cd.border = BORDER
        cd.alignment = CENTER

    ws.freeze_panes = "C4"
    ws.column_dimensions["A"].width = 13
    ws.column_dimensions["B"].width = 10
    for i in range(ticks):
        ws.column_dimensions[get_column_letter(3 + i)].width = 7.2
    ws.column_dimensions[get_column_letter(last_col)].width = 15
    ws.page_setup.orientation = "landscape"
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0

    # ---------- 工作表 2：监考安排 ----------
    ws2 = wb.create_sheet("监考安排")
    big_title(ws2, title + " · 监考安排", 6)
    total_min = sum(max(0, d.end_min - d.start_min) for d in duties)
    used = set()
    for x in duties:
        used.update(i for i in (int(y) for y in (x.teacher_ids or "").split(",") if y.strip().isdigit()) if i)
        if not (x.teacher_ids or "").strip() and x.teacher_id:
            used.add(x.teacher_id)
    sub_line(ws2, "%s  %s 开考 ｜ 共 %d 段 ｜ 参与监考 %d 人 ｜ 监考总时长 %d 分钟" % (
        p.exam_date or "", p.start_time or "", len(duties), len(used), total_min), 6)
    head_row(ws2, 4, ["考站", "时段", "监考老师", "监考时长(分钟)", "地点"])

    r = 5
    for d in sorted(duties, key=lambda y: (y.start_min, y.station_seq)):
        st = st_map.get(d.station_seq)
        names = [x for x in (d.teachers or "").split(",") if x.strip()]
        if not names:
            names = [d.teacher_name] if d.teacher_name else []
        vals = [st.name if st else "第%d站" % d.station_seq,
                "%s–%s" % (_hhmm(p.start_time, d.start_min), _hhmm(p.start_time, d.end_min)),
                "、".join(names), d.end_min - d.start_min,
                (st.location if st else "") or ""]
        for i, v in enumerate(vals, start=1):
            c = ws2.cell(row=r, column=i, value=v)
            c.font = Font(size=10)
            c.border = BORDER
            c.alignment = Alignment(horizontal="left" if i in (3, 5) else "center", vertical="center")
            if i == 1:
                c.fill = fill_of(st.color if st else "")
        ws2.row_dimensions[r].height = 20
        r += 1

    # 老师监考时长汇总
    agg = {}
    for d in duties:
        ids = [int(x) for x in (d.teacher_ids or "").split(",") if x.strip().isdigit()]
        names = [x for x in (d.teachers or "").split(",") if x.strip()]
        if not ids and d.teacher_id:
            ids = [d.teacher_id]
        if not names:
            names = [d.teacher_name or tnames.get(d.teacher_id, "")]
        for i, tid in enumerate(ids):
            key = tid or -(i + 1)
            nm = names[i] if i < len(names) else tnames.get(tid, "老师%d" % tid)
            rec = agg.setdefault(key, {"id": tid, "name": nm, "min": 0, "n": 0, "st": []})
            rec["min"] += max(0, d.end_min - d.start_min)
            rec["n"] += 1
            st = st_map.get(d.station_seq)
            rec["st"].append(st.name if st else "第%d站" % d.station_seq)

    r += 1
    ws2.cell(row=r, column=1, value="监考老师时长汇总").font = Font(size=12, bold=True, color="1F2937")
    ws2.row_dimensions[r].height = 24
    r += 1
    head_row(ws2, r, ["监考老师", "工号", "手机号", "监考时长(分钟)", "监考场次", "涉及考站"])
    r += 1
    for rec in sorted(agg.values(), key=lambda x: (-x["min"], x["name"])):
        seen, uniq = set(), []
        for q in rec["st"]:
            if q not in seen:
                seen.add(q)
                uniq.append(q)
        t = tinfo.get(rec["id"])
        vals = [rec["name"], (t.emp_id if t else "") or "", (t.phone if t else "") or "",
                rec["min"], rec["n"], "、".join(uniq)]
        for i, v in enumerate(vals, start=1):
            c = ws2.cell(row=r, column=i, value=v)
            c.font = Font(size=10)
            c.border = BORDER
            c.alignment = Alignment(horizontal="left" if i in (1, 6) else "center", vertical="center")
        ws2.row_dimensions[r].height = 20
        r += 1

    ws2.column_dimensions["A"].width = 13
    ws2.column_dimensions["B"].width = 12
    ws2.column_dimensions["C"].width = 15
    ws2.column_dimensions["D"].width = 15
    ws2.column_dimensions["E"].width = 24
    ws2.column_dimensions["F"].width = 26
    ws2.freeze_panes = "A5"

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    fname = "%s_%s.xlsx" % (title, p.exam_date or "")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename*=UTF-8''%s" % quote(fname)},
    )


@router.put("/plans/{pid}")
def update_plan(pid: int, payload: dict, s: Session = Depends(_session), _=Depends(auth.staff_only)):
    p = s.get(models.ExamPlan, pid)
    if not p:
        raise HTTPException(404, "考核计划不存在")
    for k in ("name", "exam_date", "start_time", "note", "status"):
        if k in payload:
            setattr(p, k, payload[k])
    for k in ("tick_min", "gap_min", "duty_min", "split_size", "expand_penalty"):
        if k in payload:
            setattr(p, k, int(payload[k] or 0))
    if "auto_scale" in payload:
        p.auto_scale = bool(payload["auto_scale"])
    s.add(p)
    s.commit()
    return {"ok": True}


@router.put("/plans/{pid}/stations")
def save_stations(pid: int, payload: list = Body(..., embed=False), s: Session = Depends(_session), _=Depends(auth.staff_only)):
    for row in s.exec(select(models.ExamStation).where(models.ExamStation.plan_id == pid)).all():
        s.delete(row)
    s.commit()
    for i, it in enumerate(payload):
        elastic = bool(it.get("elastic", False))
        rooms = max(1, int(it.get("rooms") or 1))
        teachers = max(1, int(it.get("teachers") or 1))
        if not elastic:
            # 1 对 1 对话站：一个考位配一位老师，两者联动
            teachers = rooms
        s.add(models.ExamStation(
            plan_id=pid, seq=int(it.get("seq") or i + 1),
            name=it.get("name") or f"第{i+1}站",
            duration_min=int(it.get("duration_min") or 10),
            rooms=rooms,
            teachers=teachers,
            location=it.get("location") or "",
            elastic=elastic,
            auto_rooms=bool(it.get("auto_rooms", False)),
            enabled=bool(it.get("enabled", True)),
            color=it.get("color") or "",
            align=bool(it.get("align", False)),
            teacher_ids=",".join(str(int(x)) for x in (it.get("teacher_ids") or []) if str(x).isdigit()),
        ))
    s.commit()
    return {"ok": True}


@router.put("/plans/{pid}/candidates")
def save_candidates(pid: int, payload: list = Body(..., embed=False), s: Session = Depends(_session), _=Depends(auth.staff_only)):
    """按 student_id 增量更新（不删表重建），保证 ExamCandidate.id 稳定。

    删重建会让 id 全部变化，前端缓存的候选与刚生成的 slots.candidate_id 对不上，
    表现为「生成后表格一片空白」。
    """
    exist = {row.student_id: row for row in
             s.exec(select(models.ExamCandidate).where(models.ExamCandidate.plan_id == pid)).all()}
    keep = set()
    for i, it in enumerate(payload):
        student_id = int(it.get("student_id") or 0)
        sts = it.get("stations")
        if isinstance(sts, (list, tuple)):
            sts = ",".join(str(int(x)) for x in sts)
        row = exist.get(student_id)
        if row is None:
            row = models.ExamCandidate(plan_id=pid, student_id=student_id)
        row.name = it.get("name") or row.name or ""
        row.group_name = it.get("group") or it.get("group_name") or row.group_name or ""
        row.stations = sts or ""
        row.sort = int(it.get("sort") if it.get("sort") is not None else i)
        row.is_out = bool(it.get("is_out", True))
        s.add(row)
        keep.add(student_id)
    for student_id, row in exist.items():      # 本次没有的（取消勾选且移出名单）清掉
        if student_id not in keep:
            s.delete(row)
    s.commit()
    return {"ok": True, "candidates": [c.dict() for c in
            s.exec(select(models.ExamCandidate).where(
                models.ExamCandidate.plan_id == pid).order_by(models.ExamCandidate.sort)).all()]}


@router.post("/plans/{pid}/generate")
def generate(pid: int, payload: dict = None, s: Session = Depends(_session), _=Depends(auth.staff_only)):
    """生成考务草稿（不落库，前端预览用）。"""
    payload = payload or {}
    p = s.get(models.ExamPlan, pid)
    if not p:
        raise HTTPException(404, "考核计划不存在")
    sts = [_station_dict(x) for x in s.exec(select(models.ExamStation).where(
        models.ExamStation.plan_id == pid).order_by(models.ExamStation.seq)).all()]
    for st in sts:
        st["enabled"] = True
    if payload.get("stations"):
        sts = payload["stations"]

    all_seqs = [x["seq"] for x in sts if x.get("enabled", True)]
    cand_rows = s.exec(select(models.ExamCandidate).where(
        models.ExamCandidate.plan_id == pid).order_by(models.ExamCandidate.sort)).all()
    cands = []
    for c in cand_rows:
        if not c.is_out:
            continue
        d = _cand_dict(c, all_seqs)
        if payload.get("candidates"):
            hit = next((x for x in payload["candidates"] if x.get("id") == c.id), None)
            if hit:
                d["stations"] = hit.get("stations") or d["stations"]
        cands.append(d)

    pool = _teacher_pool(s, p, payload.get("teacher_ids"))

    res = build_schedule(
        sts, cands, tick=p.tick_min, duty_min=p.duty_min,
        split_size=p.split_size, teachers=pool, exam_date=p.exam_date,
        gap=getattr(p, "gap_min", 10),
        expand_penalty=int(payload.get("expand_penalty",
                                       getattr(p, "expand_penalty", 20))),
        seed_runs=int(payload.get("runs") or 8),
        auto_scale=bool(payload.get("auto_scale", getattr(p, "auto_scale", True))),
        target_min=int(payload.get("target_min") or 0),
    )
    return res


@router.post("/plans/{pid}/save")
def save_result(pid: int, payload: dict, s: Session = Depends(_session), _=Depends(auth.staff_only)):
    for m in (models.ExamSlot, models.ExamDuty):
        for row in s.exec(select(m).where(m.plan_id == pid)).all():
            s.delete(row)
    s.commit()
    for it in payload.get("slots") or []:
        s.add(models.ExamSlot(
            plan_id=pid, station_seq=int(it["station_seq"]), room=int(it["room"]),
            start_min=int(it["start_min"]), end_min=int(it["end_min"]),
            candidate_id=int(it.get("candidate_id") or 0), batch=int(it.get("batch") or 0)))
    for it in payload.get("duties") or []:
        s.add(models.ExamDuty(
            plan_id=pid, station_seq=int(it["station_seq"]), room=int(it["room"]),
            start_min=int(it["start_min"]), end_min=int(it["end_min"]),
            teacher_id=int(it.get("teacher_id") or 0), teacher_name=it.get("teacher_name") or "",
            teachers=",".join(it.get("teachers") or []),
            teacher_ids=",".join(str(x) for x in (it.get("teacher_ids") or [])),
            batch=int(it.get("batch") or 0)))
    s.commit()
    p = s.get(models.ExamPlan, pid)
    if p:
        p.status = "draft"
        s.add(p)
        s.commit()
    return {"ok": True}


@router.post("/plans/{pid}/duties")
def rebuild_duties(pid: int, payload: dict, s: Session = Depends(_session),
                   _=Depends(auth.staff_only)):
    """人工调整排程后，按新的 slots 重算监考表（不重新排考生）。"""
    p = s.get(models.ExamPlan, pid)
    if not p:
        raise HTTPException(404, "考核计划不存在")
    slots = [{"station_seq": int(x["station_seq"]), "room": int(x.get("room") or 0),
              "start_min": int(x["start_min"]), "end_min": int(x["end_min"])}
             for x in (payload.get("slots") or [])]
    sts = [st.dict() for st in s.exec(select(models.ExamStation).where(
        models.ExamStation.plan_id == pid).order_by(models.ExamStation.seq)).all()]
    teachers = _teacher_pool(s, p, payload.get("teacher_ids"))
    return {"duties": _assign_duty(sts, slots, p.duty_min, teachers, p.tick_min)}


def _date_gap_days(d1: str, d2: str):
    try:
        a = datetime.strptime((d1 or "")[:10], "%Y-%m-%d").date()
        b = datetime.strptime((d2 or "")[:10], "%Y-%m-%d").date()
        return (b - a).days
    except Exception:
        return None


def _teacher_history(s, exclude_pid: int = 0):
    """按老师聚合历史监考：按月计数、考站、最近一次日期、近 90 天段数。

    用于「推荐权重」：近期（90 天内）监考多的老师派单顺序靠后。
    """
    today = datetime.now().strftime("%Y-%m-%d")
    hist = {}
    rows = s.exec(select(models.ExamDuty, models.ExamPlan).join(
        models.ExamPlan, models.ExamPlan.id == models.ExamDuty.plan_id)).all()
    for d, p in rows:
        if exclude_pid and (p.id == exclude_pid or d.plan_id == exclude_pid):
            continue
        ids = [int(x) for x in (d.teacher_ids or "").split(",") if x.strip().isdigit()]
        if not ids and d.teacher_id:
            ids = [int(d.teacher_id)]
        date = (p.exam_date or "")[:10]
        span = max(0, int(d.end_min or 0) - int(d.start_min or 0))
        for tid in set(ids):
            h = hist.setdefault(int(tid), {"total": 0, "minutes": 0, "recent90": 0,
                                           "minutes90": 0, "last": "", "months": {}})
            h["total"] += 1
            h["minutes"] += span
            ym = date[:7] or "未定"
            m = h["months"].setdefault(ym, {"count": 0, "minutes": 0, "seqs": set(), "plan": p.name or ""})
            m["count"] += 1
            m["minutes"] += span
            if d.station_seq:
                m["seqs"].add(int(d.station_seq))
            gap = _date_gap_days(date, today) if date else None
            # 未来排期视为「刚监考过」，同样降权
            if gap is None or -400 <= gap <= 90:
                h["recent90"] += 1
                h["minutes90"] += span
            if date > h["last"]:
                h["last"] = date
    return hist


def _hist_load(h):
    """把历史折算成「负担分钟」，用于派单排序：近 90 天权重最高。"""
    if not h:
        return 0.0
    return float(h.get("minutes90", 0)) * 1.0 + float(h.get("minutes", 0)) * 0.25


def _hist_score(h):
    """推荐分 0–100：近期监考越多越低；没监考过的最高。"""
    if not h:
        return 100
    # 近 90 天每段扣 20 分（最多 60），更早的历史每段扣 2 分（最多 25）
    score = 100 - min(60, int(h.get("recent90", 0)) * 20) - min(25, max(0, int(h.get("total", 0)) - int(h.get("recent90", 0))) * 2)
    return max(5, score)


def _teacher_pool(s, p, picked=None):
    """可选监考老师：勾选范围内、当天没请假的；带历史负担 load（越轻越先派）。"""
    hist = _teacher_history(s, exclude_pid=(p.id if p else 0))
    out = []
    for t in s.exec(select(models.Teacher)).all():
        if picked is not None and t.id not in picked:
            continue
        if p and p.exam_date and p.exam_date in (t.leave_dates or ""):
            continue
        h = hist.get(int(t.id)) or {}
        out.append({"id": t.id, "name": t.name, "phone": t.phone or "",
                    "load": _hist_load(h), "recent90": int(h.get("recent90", 0)),
                    "total": int(h.get("total", 0))})
    out.sort(key=lambda x: (x["load"], x["recent90"], x["id"]))
    return out


@router.get("/teacher-load")
def teacher_load(plan_id: int = 0, s: Session = Depends(_session), _=Depends(auth.staff_only)):
    """考官名单 + 历史监考（月份 / 考站 / 段数）+ 推荐权重（近期监考过的降权）。"""
    p = s.get(models.ExamPlan, plan_id) if plan_id else None
    hist = _teacher_history(s, exclude_pid=plan_id)
    st_names = {}
    if p:
        for st in s.exec(select(models.ExamStation).where(models.ExamStation.plan_id == plan_id)).all():
            st_names[int(st.seq)] = st.name
    out = []
    for t in s.exec(select(models.Teacher)).all():
        h = hist.get(int(t.id)) or {}
        months = []
        for ym in sorted(h.get("months", {}).keys(), reverse=True)[:6]:
            m = h["months"][ym]
            months.append({"ym": ym, "count": m["count"], "minutes": m["minutes"],
                           "stations": [st_names.get(sq, "第%d站" % sq) for sq in sorted(m["seqs"])],
                           "plan": m.get("plan", "")})
        out.append({
            "id": t.id, "name": t.name, "phone": t.phone or "",
            "leave_dates": t.leave_dates or "",
            "leave": bool(p and p.exam_date and p.exam_date in (t.leave_dates or "")),
            "total": int(h.get("total", 0)), "minutes": int(h.get("minutes", 0)),
            "recent90": int(h.get("recent90", 0)), "last_date": h.get("last", ""),
            "months": months,
            "station_names": sorted({n for m in months for n in m["stations"]}),
            "score": _hist_score(h), "load": _hist_load(h),
        })
    out.sort(key=lambda x: (-x["score"], x["load"], x["id"]))
    return {"teachers": out, "today": datetime.now().strftime("%Y-%m-%d")}


def _hhmm(start_time, minutes):
    parts = (start_time or "12:30").split(":")
    base = (int(parts[0]) if parts[0].isdigit() else 12) * 60 + (int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0)
    t = base + int(minutes)
    return "%02d:%02d" % (t // 60 % 24, t % 60)


def _exam_teacher_msg(p, duties, st_map):
    lines = ["【技能考核】" + (p.name or "技能考核"),
             "日期：%s %s 开始" % (p.exam_date or "", p.start_time or "")]
    for d in sorted(duties, key=lambda x: x.start_min):
        st = st_map.get(d.station_seq)
        loc = ("｜" + st.location) if st and st.location else ""
        lines.append("· %s %s–%s%s" % ((st.name if st else "第%d站" % d.station_seq),
                                       _hhmm(p.start_time, d.start_min),
                                       _hhmm(p.start_time, d.end_min), loc))
    lines.append("请提前 10 分钟到岗，谢谢！")
    return "\n".join(lines)


def _exam_student_msg(p, slots, st_map):
    lines = ["【技能考核】" + (p.name or "技能考核"),
             "日期：%s，开考 %s，请提前 10 分钟到达。" % (p.exam_date or "", p.start_time or "")]
    for x in sorted(slots, key=lambda y: y.start_min):
        st = st_map.get(x.station_seq)
        lines.append("· %s–%s %s%s" % (_hhmm(p.start_time, x.start_min),
                                       _hhmm(p.start_time, x.end_min),
                                       (st.name if st else "第%d站" % x.station_seq),
                                       ("（" + st.location + "）") if st and st.location else ""))
    lines.append("请带好学生证，按顺序轮转。")
    return "\n".join(lines)


@router.post("/plans/{pid}/publish")
def publish(pid: int, payload: dict = None, s: Session = Depends(_session),
            _=Depends(auth.staff_only)):
    """发布考务：状态置为已发布，并给监考老师、考生各推一条钉钉单聊。"""
    payload = payload or {}
    p = s.get(models.ExamPlan, pid)
    if not p:
        raise HTTPException(404, "考核计划不存在")
    slots = s.exec(select(models.ExamSlot).where(models.ExamSlot.plan_id == pid)).all()
    if not slots:
        raise HTTPException(400, "还没有排程结果，请先生成并保存草稿")
    p.status = "published"
    s.add(p)
    s.commit()

    report = {"ok": True, "teachers": {"sent": [], "failed": [], "skipped": []},
              "students": {"sent": [], "failed": [], "skipped": []}, "note": ""}
    if payload.get("notify") is False:
        report["note"] = "已发布（按要求未推送）"
        return report

    st_map = {st.seq: st for st in s.exec(select(models.ExamStation).where(
        models.ExamStation.plan_id == pid)).all()}
    duties = s.exec(select(models.ExamDuty).where(models.ExamDuty.plan_id == pid)).all()
    cands = {c.id: c for c in s.exec(select(models.ExamCandidate).where(
        models.ExamCandidate.plan_id == pid)).all()}

    try:
        from api import _dt_access_token, _dt_userid, _dt_send_dm
    except Exception as e:                                  # pragma: no cover
        report["ok"] = False
        report["note"] = "推送模块不可用：" + str(e)[:120]
        return report
    cfg = s.exec(select(models.DingTalkConfig)).first()
    if not cfg or not cfg.app_key or not cfg.app_secret:
        report["ok"] = False
        report["note"] = "未配置钉钉企业应用 AppKey/AppSecret，已发布但未推送"
        return report
    try:
        token = _dt_access_token(cfg)
    except Exception as e:
        report["ok"] = False
        report["note"] = "获取 access_token 失败：" + str(e)[:120]
        return report

    # ---- 监考老师 ----
    by_teacher = {}
    for d in duties:
        ids = [int(x) for x in (d.teacher_ids or "").split(",") if x.strip().isdigit()]
        if not ids and d.teacher_id:
            ids = [d.teacher_id]
        for tid in ids:
            by_teacher.setdefault(tid, []).append(d)
    teachers = {t.id: t for t in s.exec(select(models.Teacher)).all()}
    for tid, ds in by_teacher.items():
        t = teachers.get(tid)
        if not t:
            continue
        name = t.name
        mobile = (t.phone or "").strip()
        if not mobile:
            report["teachers"]["skipped"].append({"name": name, "reason": "未登记手机号"})
            continue
        try:
            uid = _dt_userid(token, mobile)
            _dt_send_dm(cfg, token, uid, _exam_teacher_msg(p, ds, st_map))
            report["teachers"]["sent"].append(name)
        except Exception as e:
            report["teachers"]["failed"].append({"name": name, "error": str(e)[:100]})

    # ---- 考生 ----
    by_stu = {}
    for x in slots:
        by_stu.setdefault(x.candidate_id, []).append(x)
    for cid, xs in by_stu.items():
        c = cands.get(cid)
        if not c:
            continue
        stu = s.get(models.Student, c.student_id)
        mobile = ((stu.phone if stu else "") or "").strip()
        if not mobile:
            report["students"]["skipped"].append({"name": c.name, "reason": "未登记手机号"})
            continue
        try:
            uid = _dt_userid(token, mobile)
            _dt_send_dm(cfg, token, uid, _exam_student_msg(p, xs, st_map))
            report["students"]["sent"].append(c.name)
        except Exception as e:
            report["students"]["failed"].append({"name": c.name, "error": str(e)[:100]})

    report["note"] = "老师 %d 人、考生 %d 人已推送；失败 %d、缺手机号 %d" % (
        len(report["teachers"]["sent"]), len(report["students"]["sent"]),
        len(report["teachers"]["failed"]) + len(report["students"]["failed"]),
        len(report["teachers"]["skipped"]) + len(report["students"]["skipped"]))
    report["ok"] = not (report["teachers"]["failed"] or report["students"]["failed"])
    return report
