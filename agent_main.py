import os
import sys
import json
from datetime import datetime, timedelta, time
from typing import Dict, Any, List, Tuple

# 数据时间戳基于 CST (Asia/Shanghai, UTC+8)，确保本地时区一致
os.environ.setdefault("TZ", "Asia/Shanghai")
try:
    import time as _time_mod
    _time_mod.tzset()
except AttributeError:
    pass  # Windows 没有 tzset

import pandas as pd
from openai import OpenAI

# =========================
# 1. 数据源选择: CSV 或 MySQL
# =========================
# 优先级:
#   1. 命令行参数 --csv <目录>
#   2. 环境变量 DATA_MODE=csv  +  CSV_DIR=./env_huayao_tables
#   3. 默认使用 MySQL

DATA_MODE = os.environ.get("DATA_MODE", "mysql").lower()
CSV_DIR   = os.environ.get("CSV_DIR", "./env_huayao_tables")

# 检查命令行参数
if "--csv" in sys.argv:
    DATA_MODE = "csv"
    idx = sys.argv.index("--csv")
    if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("--"):
        CSV_DIR = sys.argv[idx + 1]

if DATA_MODE == "csv":
    from csv_engine import CsvEngine
    engine = CsvEngine(CSV_DIR)

    # 提供一个兼容的 text() 函数，CSV 模式下 SQL 字符串直接透传
    def text(s):
        return s
else:
    from sqlalchemy import create_engine, text

    USER    = "root"
    PASS    = os.environ.get("MYSQL_PASS", "fighting")
    DB      = "env_huayao"
    SOCKET  = "/tmp/mysql.sock"

    engine = create_engine(
        f"mysql+pymysql://{USER}:{PASS}@localhost/{DB}?unix_socket={SOCKET}",
        pool_pre_ping=True
    )


LESSON_MINUTES = 90  # 一节课 1.5 小时

# 固定可用的上课时间槽（每天）
ALLOWED_SLOTS = [
    ("09:00:00", "10:30:00"),
    ("10:30:00", "12:00:00"),
    ("13:30:00", "15:00:00"),
    ("15:00:00", "16:30:00"),
    ("16:30:00", "18:00:00"),
    ("18:00:00", "19:30:00"),
]

def build_slot_dt(slot_date: datetime.date, slot: Tuple[str, str]) -> Tuple[datetime, datetime]:
    start_str, end_str = slot
    slot_start = datetime.combine(slot_date, time.fromisoformat(start_str.strip()))
    slot_end = datetime.combine(slot_date, time.fromisoformat(end_str.strip()))
    return slot_start, slot_end

def intervals_overlap(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return not (a_end <= b_start or b_end <= a_start)

from datetime import datetime, timedelta
from typing import Any, Dict, List, Tuple, Optional

# 你原来就有的：
# ALLOWED_SLOTS = [...]
# build_slot_dt(...)

def get_conflicting_lessons_for_group(
    engine,
    teacher_id: int,
    student_ids: List[int],
    slot_start_dt: datetime,
    slot_end_dt: datetime,
) -> List[Dict[str, Any]]:
    """
    查出在 [slot_start_dt, slot_end_dt] 时间段内，
    老师 teacher_id 或 student_ids 中任意一个学生有课的所有 lesson 记录。

    返回的每个 lesson 字典至少包含：
      - lesson_id
      - lesson_start / lesson_end
      - subject_id, class_id, teacher_id, class_name
      - topic_name / topic_cn_name / topic（方便人类阅读）
    """
    start_unix = int(slot_start_dt.timestamp())
    end_unix = int(slot_end_dt.timestamp())

    results: List[Dict[str, Any]] = []

    with engine.connect() as conn:
        # 1) 老师的冲突课
        teacher_sql = text("""
            SELECT
                l.id                                                            AS lesson_id,
                IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                IF(l.end_time   = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))   AS lesson_end,
                s.id                                                            AS subject_id,
                s.class_id                                                      AS class_id,
                s.teacher_id                                                    AS teacher_id,
                c.class_type                                                    AS class_name,
                t.name                                                          AS topic_name,
                t.cn_name                                                       AS topic_cn_name
            FROM lessons l
            JOIN subjects s ON l.subject_id = s.id
            JOIN classes  c ON s.class_id  = c.id
            LEFT JOIN topics t ON s.topic_id = t.id
            WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
               AND (l.end_time   = -1 OR l.end_time   >= :start_unix))
              AND s.teacher_id = :tid
        """)
        teacher_rows = conn.execute(
            teacher_sql,
            {"start_unix": start_unix, "end_unix": end_unix, "tid": teacher_id},
        ).mappings().all()
        results.extend(dict(r) for r in teacher_rows)

        # 2) 学生的冲突课（任一学生）
        if student_ids:
            student_sql = text("""
                SELECT
                    l.id                                                            AS lesson_id,
                    IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                    IF(l.end_time   = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))   AS lesson_end,
                    s.id                                                            AS subject_id,
                    s.class_id                                                      AS class_id,
                    s.teacher_id                                                    AS teacher_id,
                    c.class_type                                                    AS class_name,
                    t.name                                                          AS topic_name,
                    t.cn_name                                                       AS topic_cn_name
                FROM lessons l
                JOIN subjects s ON l.subject_id = s.id
                JOIN classes  c ON s.class_id  = c.id
                JOIN student_classes sc ON sc.class_id = c.id
                LEFT JOIN topics t ON s.topic_id = t.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                   AND (l.end_time   = -1 OR l.end_time   >= :start_unix))
                  AND sc.student_id IN :sids
            """)
            student_rows = conn.execute(
                student_sql,
                {"start_unix": start_unix, "end_unix": end_unix, "sids": tuple(student_ids)},
            ).mappings().all()
            results.extend(dict(r) for r in student_rows)

    # 3) 按 lesson_id 去重，并补一个 topic 字段
    dedup: Dict[int, Dict[str, Any]] = {}
    for row in results:
        lid = row.get("lesson_id")
        if lid is None:
            continue
        if lid not in dedup:
            topic_name = row.get("topic_name") or ""
            topic_cn_name = row.get("topic_cn_name") or ""
            row["topic"] = f"{topic_name}{topic_cn_name}"
            dedup[lid] = row

    return list(dedup.values())


def get_ids(engine, teacher_name: str, student_name: str) -> Tuple[int, int]:
    """
    把姓名映射成 teacher_id / student_id。
    staff / students 里用的是 name_search_cache。
    """
    with engine.connect() as conn:
        teacher_id = conn.execute(
            text("""
                SELECT id 
                FROM staff 
                WHERE name_search_cache = :name
                LIMIT 1
            """),
            {"name": teacher_name}
        ).scalar()

        student_id = conn.execute(
            text("""
                SELECT id 
                FROM students 
                WHERE name_search_cache = :name
                LIMIT 1
            """),
            {"name": student_name}
        ).scalar()

    return teacher_id, student_id

def check_schedule(
    engine,
    start_time_str: str,
    end_time_str: str,
    teacher_name: str,
    student_name: str,
):
    """
    原来的 check_schedule，略微改成更容易给 LLM 使用：
    - 保留原来的 SQL 逻辑
    - 多返回结构化数据，少用 print
    """
    start_dt = datetime.fromisoformat(start_time_str)
    end_dt = datetime.fromisoformat(end_time_str)
    start_unix = int(start_dt.timestamp())
    end_unix = int(end_dt.timestamp())

    teacher_id, student_id = get_ids(engine, teacher_name, student_name)

    def _fmt_time(ts):
        if ts is None:
            return "NULL"
        if ts == -1:
            return "PERMANENT(-1)"
        try:
            return datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            return f"RAW({ts})"

    with engine.connect() as conn:
        # ----- 学生在该时间的课 -----
        student_rows = []
        if student_id is not None:
            student_classes_sql = text("""
                SELECT l.id                                                            AS lesson_id,
                       IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                       IF(l.end_time = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))     AS lesson_end,
                       s.id                                                            AS subject_id,
                       s.class_id                                                      AS class_id,
                       s.teacher_id                                                    AS teacher_id,
                       c.class_type                                                    AS class_name,
                       sc.start_time                                                   AS sc_start_time,
                       sc.end_time                                                     AS sc_end_time,
                       t.name                                                          AS topic_name,
                       t.cn_name                                                       AS topic_cn_name
                FROM lessons l
                         JOIN subjects s ON l.subject_id = s.id
                         JOIN classes c ON s.class_id = c.id
                         JOIN student_classes sc ON sc.class_id = s.class_id
                         LEFT JOIN topics t ON s.topic_id = t.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                    AND (l.end_time = -1 OR l.end_time >= :start_unix))
                  AND ((sc.start_time = -1 OR sc.start_time <= :end_unix)
                    AND (sc.end_time = -1 OR sc.end_time >= :start_unix))
                  AND sc.student_id = :student_id
                ORDER BY l.start_time
            """)

            student_rows = conn.execute(
                student_classes_sql,
                {
                    "start_unix": start_unix,
                    "end_unix": end_unix,
                    "student_id": student_id,
                },
            ).mappings().all()

        # ----- 老师在该时间的课 -----
        teacher_rows = []
        if teacher_id is not None:
            teacher_classes_sql = text("""
                SELECT l.id                                                            AS lesson_id,
                       IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                       IF(l.end_time = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))     AS lesson_end,
                       s.id                                                            AS subject_id,
                       s.class_id                                                      AS class_id,
                       s.teacher_id                                                    AS teacher_id,
                       c.class_type                                                    AS class_name,
                       t.name                                                          AS topic_name,
                       t.cn_name                                                       AS topic_cn_name
                FROM lessons l
                         JOIN subjects s ON l.subject_id = s.id
                         JOIN classes c ON s.class_id = c.id
                         LEFT JOIN topics t ON s.topic_id = t.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                    AND (l.end_time = -1 OR l.end_time >= :start_unix))
                  AND s.teacher_id = :teacher_id
            """)

            teacher_rows = conn.execute(
                teacher_classes_sql,
                {
                    "start_unix": start_unix,
                    "end_unix": end_unix,
                    "teacher_id": teacher_id,
                },
            ).mappings().all()

    # 结构化返回
    student_time_desc = []
    student_struct = []
    if not student_rows:
        student_time_desc.append("No student course.")
    else:
        for r in student_rows:
            row = dict(r)
            topic_name = row.get("topic_name") or ""
            topic_cn_name = row.get("topic_cn_name") or ""
            row["topic"] = f"{topic_name}{topic_cn_name}"
            student_struct.append(row)
            student_time_desc.append(
                f"{row['lesson_id']}:{ _fmt_time(row['lesson_start']) } ~ { _fmt_time(row['lesson_end']) }"
            )

    teacher_time_desc = []
    teacher_struct = []
    if not teacher_rows:
        teacher_time_desc.append("No teacher course.")
    else:
        for r in teacher_rows:
            row = dict(r)
            topic_name = row.get("topic_name") or ""
            topic_cn_name = row.get("topic_cn_name") or ""
            row["topic"] = f"{topic_name}{topic_cn_name}"
            teacher_struct.append(row)
            teacher_time_desc.append(
                f"{row['topic']}:{ row['lesson_start'] } ~ { row['lesson_end'] }"
            )

    return {
        "student_struct": student_struct,
        "teacher_struct": teacher_struct,
        "student_time_desc": student_time_desc,
        "teacher_time_desc": teacher_time_desc,
        "teacher_id": teacher_id,
        "student_id": student_id,
        "start_time": start_time_str,
        "end_time": end_time_str,
    }


# =========================================================
# query_lesson_schedule: 核心查询 — 给定时间+老师+学生，查课
# =========================================================

def query_lesson_schedule(
    engine,
    date_str: str,
    slot_index: int,
    teacher_id: int,
    student_id: int,
) -> Dict[str, Any]:
    """
    输入: 日期 (YYYY-MM-DD), timeslot 索引 (0-5 对应 ALLOWED_SLOTS), teacher_id, student_id
    输出:
    {
        "slot_start": "2025-11-13 09:00:00",
        "slot_end":   "2025-11-13 10:30:00",
        "student_has_class": True/False,
        "student_lessons": [ {lesson_id, class_name, topic, teacher_id, teacher_name, ...} ],
        "teacher_has_class": True/False,
        "teacher_lessons": [ {lesson_id, class_name, topic, class_id, students_in_class: [...], ...} ],
    }

    流程:
    1. lessons.start_time/end_time 匹配时间 → 取 subject_id
    2. subject_id → subjects.class_id, subjects.teacher_id
    3. class_id → classes.class_type (课名)
    4. class_id → student_classes 里筛选 student_id (enrollment 时间要 cover 查询时间)
    """
    if slot_index < 0 or slot_index >= len(ALLOWED_SLOTS):
        return {"error": f"slot_index 必须在 0~{len(ALLOWED_SLOTS)-1} 之间"}

    slot = ALLOWED_SLOTS[slot_index]
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    slot_start_dt, slot_end_dt = build_slot_dt(target_date, slot)
    start_unix = int(slot_start_dt.timestamp())
    end_unix = int(slot_end_dt.timestamp())

    result = {
        "slot_start": slot_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "slot_end": slot_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "student_has_class": False,
        "student_lessons": [],
        "teacher_has_class": False,
        "teacher_lessons": [],
    }

    with engine.connect() as conn:
        # ---- 学生在该时间段的课 ----
        student_sql = text("""
            SELECT
                l.id            AS lesson_id,
                l.start_time    AS lesson_start_unix,
                l.end_time      AS lesson_end_unix,
                s.id            AS subject_id,
                s.class_id      AS class_id,
                s.teacher_id    AS teacher_id,
                c.class_type    AS class_name,
                t.name          AS topic_name,
                t.cn_name       AS topic_cn_name,
                stf.name_search_cache AS teacher_name
            FROM lessons l
            JOIN subjects s ON l.subject_id = s.id
            JOIN classes c ON s.class_id = c.id
            JOIN student_classes sc ON sc.class_id = s.class_id
            LEFT JOIN topics t ON s.topic_id = t.id
            LEFT JOIN staff stf ON s.teacher_id = stf.id
            WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
               AND (l.end_time = -1 OR l.end_time >= :start_unix))
              AND ((sc.start_time = -1 OR sc.start_time <= :end_unix)
               AND (sc.end_time = -1 OR sc.end_time >= :start_unix))
              AND sc.student_id = :student_id
            ORDER BY l.start_time
        """)
        student_rows = conn.execute(
            student_sql,
            {"start_unix": start_unix, "end_unix": end_unix, "student_id": student_id},
        ).mappings().all()

        seen_lessons = set()
        for r in student_rows:
            row = dict(r)
            lid = row["lesson_id"]
            if lid in seen_lessons:
                continue
            seen_lessons.add(lid)
            row["topic"] = (row.get("topic_name") or "") + (row.get("topic_cn_name") or "")
            result["student_lessons"].append(row)

        result["student_has_class"] = len(result["student_lessons"]) > 0

        # ---- 老师在该时间段的课 ----
        teacher_sql = text("""
            SELECT
                l.id            AS lesson_id,
                l.start_time    AS lesson_start_unix,
                l.end_time      AS lesson_end_unix,
                s.id            AS subject_id,
                s.class_id      AS class_id,
                s.teacher_id    AS teacher_id,
                c.class_type    AS class_name,
                t.name          AS topic_name,
                t.cn_name       AS topic_cn_name
            FROM lessons l
            JOIN subjects s ON l.subject_id = s.id
            JOIN classes c ON s.class_id = c.id
            LEFT JOIN topics t ON s.topic_id = t.id
            WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
               AND (l.end_time = -1 OR l.end_time >= :start_unix))
              AND s.teacher_id = :teacher_id
            ORDER BY l.start_time
        """)
        teacher_rows = conn.execute(
            teacher_sql,
            {"start_unix": start_unix, "end_unix": end_unix, "teacher_id": teacher_id},
        ).mappings().all()

        seen_lessons = set()
        for r in teacher_rows:
            row = dict(r)
            lid = row["lesson_id"]
            if lid in seen_lessons:
                continue
            seen_lessons.add(lid)
            row["topic"] = (row.get("topic_name") or "") + (row.get("topic_cn_name") or "")

            # 附加：该课涉及哪些学生
            class_id = row["class_id"]
            sc_rows = conn.execute(
                text("""
                    SELECT sc.student_id, stu.name_search_cache AS student_name
                    FROM student_classes sc
                    LEFT JOIN students stu ON sc.student_id = stu.id
                    WHERE sc.class_id = :cid
                      AND ((sc.start_time = -1 OR sc.start_time <= :end_unix)
                       AND (sc.end_time = -1 OR sc.end_time >= :start_unix))
                """),
                {"cid": class_id, "start_unix": start_unix, "end_unix": end_unix},
            ).mappings().all()
            row["students_in_class"] = [
                {"student_id": s["student_id"], "student_name": s["student_name"]}
                for s in sc_rows
            ]
            result["teacher_lessons"].append(row)

        result["teacher_has_class"] = len(result["teacher_lessons"]) > 0

    return result


def direct_check_and_plan(
    engine,
    student_name: str,
    teacher_name: str,
    intent_start: str,
    intent_end: str,
) -> Dict[str, Any]:
    """
    第一步：用纯算法检查当前老师是否在该时间有冲突。
    返回：
    {
        "status": "ok" / "teacher_busy" / "student_busy" / "both_busy",
        "check_result": check_schedule(...) 的原始结果
    }
    """
    result = check_schedule(
        engine,
        start_time_str=intent_start,
        end_time_str=intent_end,
        teacher_name=teacher_name,
        student_name=student_name,
    )

    student_has_class = len(result["student_struct"]) > 0
    teacher_has_class = len(result["teacher_struct"]) > 0

    if not student_has_class and not teacher_has_class:
        status = "ok"
    elif teacher_has_class and not student_has_class:
        status = "teacher_busy"
    elif student_has_class and not teacher_has_class:
        status = "student_busy"
    else:
        status = "both_busy"

    return {
        "status": status,
        "check_result": result,
    }

def fetch_all_topics(engine) -> List[Dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT id, name, cn_name FROM topics")
        ).mappings().all()
    return [dict(r) for r in rows]

def llm_select_topic_ids(client, model: str, topics: List[Dict[str, Any]], requirement: str) -> List[int]:
    """
    把 topics 全表和「排课需求描述」一起丢给 LLM，让它输出一个 topic_id 列表。
    """
    prompt = f"""
你是一个排课助手。下面是 topics 表的全部行：

{json.dumps(topics, ensure_ascii=False, indent=2)}

排课需求为（自然语言）：{requirement}

请你判断，这个排课需求对应的是哪些 topic_id（可能是 1 个，也可能是多个）。
只输出一个 JSON，格式如下：

{{
  "topic_ids": [1, 2, 3]
}}

如果你不确定，就尽量根据 name / cn_name 猜最可能的几个。
"""

    resp = client.responses.create(
        model=model,
        input=prompt,
    )
    text_out = resp.output[0].content[0].text.strip()
    try:
        parsed = json.loads(text_out)
        topic_ids = parsed.get("topic_ids", [])
        topic_ids = [int(x) for x in topic_ids]
        return topic_ids
    except Exception:
        # 防御：如果解析失败，直接返回空列表
        return []

def fetch_teachers_for_topics(engine, topic_ids: List[int]) -> List[int]:
    """
    在 subjects 表里，根据 topic_ids 找到所有涉及到的 teacher_id。
    去重返回。
    """
    if not topic_ids:
        return []

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT teacher_id
                FROM subjects
                WHERE topic_id IN :topic_ids
                  AND teacher_id IS NOT NULL
            """),
            {"topic_ids": tuple(topic_ids)}
        ).fetchall()

    teacher_ids = [r[0] for r in rows]
    return teacher_ids

def fetch_teacher_names(engine, teacher_ids: List[int]) -> Dict[int, str]:
    """
    返回 {teacher_id: name} 映射。
    这里用 staff 表里的 name_search_cache 作为老师名字。
    """
    if not teacher_ids:
        return {}

    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT id, name_search_cache AS name
                FROM staff
                WHERE id IN :ids
            """),
            {"ids": tuple(teacher_ids)}
        ).mappings().all()

    return {int(r["id"]): r["name"] for r in rows}

def check_all_candidate_teachers(
    engine,
    student_name: str,
    teacher_map: Dict[int, str],
    intent_start: str,
    intent_end: str,
) -> List[Dict[str, Any]]:
    """
    对候选 teacher_id/teacher_name 逐个调用 check_schedule。
    返回一个列表，每个元素是：
    {
      "teacher_id": ...,
      "teacher_name": "...",
      "check_result": {...},
      "status": "ok" / "teacher_busy" / "both_busy" / ...
    }
    """
    results = []
    for tid, tname in teacher_map.items():
        r = check_schedule(
            engine,
            start_time_str=intent_start,
            end_time_str=intent_end,
            teacher_name=tname,
            student_name=student_name,
        )
        teacher_has_class = len(r["teacher_struct"]) > 0
        student_has_class = len(r["student_struct"]) > 0

        if not teacher_has_class and not student_has_class:
            status = "ok"
        elif teacher_has_class and not student_has_class:
            status = "teacher_busy"
        elif student_has_class and not teacher_has_class:
            status = "student_busy"
        else:
            status = "both_busy"

        results.append({
            "teacher_id": tid,
            "teacher_name": tname,
            "check_result": r,
            "status": status,
        })
    return results

# def llm_choose_teacher_from_candidates(
#     client,
#     model: str,
#     requirement: str,
#     candidate_results: List[Dict[str, Any]],
# ) -> Dict[str, Any]:
#     """
#     把所有候选老师在这个时间段的空闲/忙碌情况传给 LLM，让它选一个最合适的。
#     返回：
#     {
#       "chosen_teacher_id": ... 或 None,
#       "final_text": "给管理员看的文字说明"
#     }
#     """
#     prompt = f"""
# 你是一个排课助手。排课需求：{requirement}
#
# 下面是所有候选老师在目标时间段内的排班情况（JSON）：
#
# {json.dumps(candidate_results, ensure_ascii=False, indent=2)}
#
# 请你根据这些信息，判断：
# 1. 如果存在至少一位老师 status == "ok"，请选择其中一个最合适的老师（可以考虑不要太多冲突课之类的简单原则）。
# 2. 如果所有老师都不能排（没有 "ok"），就说明无法通过换老师的方式解决。
#
# 输出 JSON，格式如下：
#
# {{
#   "chosen_teacher_id": 123,   // 如果没有合适老师，写 null
#   "final_text": "用中文给排课管理员的一段解释：包括你选了哪个老师，为什么；如果不行，说明原因。"
# }}
# """
#
#     resp = client.responses.create(
#         model=model,
#         input=prompt,
#     )
#     text_out = resp.output[0].content[0].text.strip()
#     try:
#         parsed = json.loads(text_out)
#         return parsed
#     except Exception:
#         return {
#             "chosen_teacher_id": None,
#             "final_text": f"LLM 输出无法解析，原始输出：\n{text_out}"
#         }

def change_teacher_strategy(
    engine,
    client,
    model: str,
    student_name: str,
    requirement: str,
    intent_start: str,
    intent_end: str,
) -> Dict[str, Any]:
    """
    方案一：换老师。
    返回：
    {
      "success": True/False,
      "final_text": "...",
      "chosen_teacher_name": "... 或 None"
    }
    """
    # 1. 拿 topics 全表
    topics = fetch_all_topics(engine)

    # 2. 让 LLM 从 topics 里选出相关 topic_id
    topic_ids = llm_select_topic_ids(client, model, topics, requirement)

    if not topic_ids:
        return {
            "success": False,
            "final_text": "无法从 topics 表中识别出与该排课需求对应的 topic_id，换老师策略失败。",
            "chosen_teacher_name": None,
        }

    # 3. 根据 topic_id 找所有可能的 teacher_id
    teacher_ids = fetch_teachers_for_topics(engine, topic_ids)
    teacher_map = fetch_teacher_names(engine, teacher_ids)

    if not teacher_map:
        return {
            "success": False,
            "final_text": "根据对应的 topic_id 未能找到任何老师，换老师策略失败。",
            "chosen_teacher_name": None,
        }

    # 4. 对这些老师逐个 check_schedule
    candidate_results = check_all_candidate_teachers(
        engine,
        student_name=student_name,
        teacher_map=teacher_map,
        intent_start=intent_start,
        intent_end=intent_end,
    )

    # # 5. 再交给 LLM 选一个老师（或声明不行）
    # choice = llm_choose_teacher_from_candidates(
    #     client,
    #     model,
    #     requirement,
    #     candidate_results,
    # )

    # 找所有能直接排课的老师，而不是只选一个
    available_teachers = [
        item for item in candidate_results
        if item["status"] == "ok"
    ]
    return {
        "success": len(available_teachers) > 0,
        "candidates": available_teachers,
    }

    chosen_id = choice.get("chosen_teacher_id")
    chosen_name = teacher_map.get(chosen_id) if chosen_id is not None else None
    final_text = choice.get("final_text", "")

    return {
        "success": chosen_name is not None,
        "final_text": final_text,
        "chosen_teacher_name": chosen_name,
    }

def get_class_participants(engine, class_id: int) -> Tuple[int, List[int]]:
    """
    给一个 class_id，返回：
    - 这门课的 teacher_id
    - 这门课所有学生的 student_id 列表
    """
    with engine.connect() as conn:
        # teacher_id 在 subjects 表里
        teacher_id = conn.execute(
            text("SELECT teacher_id FROM subjects WHERE class_id = :cid LIMIT 1"),
            {"cid": class_id},
        ).scalar()

        # 学生在 student_classes 表里
        rows = conn.execute(
            text("SELECT DISTINCT student_id FROM student_classes WHERE class_id = :cid"),
            {"cid": class_id},
        ).fetchall()

    student_ids = [r[0] for r in rows]
    return teacher_id, student_ids

def is_group_free(
    engine,
    teacher_id: int,
    student_ids: List[int],
    start_unix: int,
    end_unix: int,
) -> bool:
    """
    检查一个老师 + 多个学生，在给定时间段内是否都没有课。
    为了简单，用两次 SQL：
    1. 检查 teacher lessons
    2. 检查 students lessons（IN (...)）
    """
    with engine.connect() as conn:
        # 1）老师有没有课
        teacher_conflict = conn.execute(
            text("""
                SELECT COUNT(*) FROM lessons l
                JOIN subjects s ON l.subject_id = s.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                   AND (l.end_time = -1 OR l.end_time >= :start_unix))
                  AND s.teacher_id = :tid
            """),
            {"start_unix": start_unix, "end_unix": end_unix, "tid": teacher_id},
        ).scalar()

        if teacher_conflict and teacher_conflict > 0:
            return False

        if not student_ids:
            # 没学生也可以视为没冲突
            return True

        # 2）学生有没有课
        # NOTE: 这里假设 student_classes -> classes -> subjects -> lessons 的关系；
        # 简化写法，可以根据你的真实 schema 调整。
        student_conflict = conn.execute(
            text("""
                SELECT COUNT(*) 
                FROM lessons l
                JOIN subjects s ON l.subject_id = s.id
                JOIN classes c ON s.class_id = c.id
                JOIN student_classes sc ON sc.class_id = c.id
                WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                   AND (l.end_time = -1 OR l.end_time >= :start_unix))
                  AND sc.student_id IN :sids
            """),
            {"start_unix": start_unix, "end_unix": end_unix, "sids": tuple(student_ids)},
        ).scalar()

        if student_conflict and student_conflict > 0:
            return False

    return True

from datetime import datetime, timedelta

def find_future_slot_for_class_discrete(
    engine,
    teacher_id: int,
    student_ids: List[int],
    from_dt: datetime,
    horizon_days: int = 30,
) -> Tuple[datetime, datetime] | Tuple[None, None]:
    """
    在 from_dt 之后 horizon_days 的范围内，寻找一个所有人都空的 slot。
    slot 固定为 ALLOWED_SLOTS 中的 90 分钟时间段。
    """
    for day_offset in range(horizon_days):
        day = (from_dt + timedelta(days=day_offset)).date()

        for slot in ALLOWED_SLOTS:
            slot_start_dt, slot_end_dt = build_slot_dt(day, slot)

            # 不能排到 from_dt 之前
            if slot_end_dt <= from_dt:
                continue

            start_unix = int(slot_start_dt.timestamp())
            end_unix = int(slot_end_dt.timestamp())

            if is_group_free(engine, teacher_id, student_ids, start_unix, end_unix):
                return slot_start_dt, slot_end_dt
    return None, None


def enumerate_future_slots_for_group(
    engine,
    teacher_id: int,
    student_ids: List[int],
    from_dt: datetime,
    horizon_days: int = 7,
) -> List[Tuple[datetime, datetime]]:
    slots = []
    for day_offset in range(horizon_days):
        day = (from_dt + timedelta(days=day_offset)).date()
        if day.weekday() >= 5:  # 跳周末
            continue
        for slot in ALLOWED_SLOTS:
            s, e = build_slot_dt(day, slot)
            if e <= from_dt:
                continue
            slots.append((s, e))
    # 这里暂时不做排序优化，你可以按时间自然顺序就行
    return slots

def enumerate_future_slots(
    from_dt: datetime,
    horizon_days: int = 7,
) -> List[Tuple[datetime, datetime]]:
    """
    列出 from_dt 之后 horizon_days 内所有工作日的 ALLOWED_SLOTS。
    不检查是否空闲，只是产生候选时间段。
    """
    slots: List[Tuple[datetime, datetime]] = []
    for day_offset in range(horizon_days):
        day = (from_dt + timedelta(days=day_offset)).date()
        # 跳过周末：Monday=0,...,Sunday=6
        if day.weekday() >= 5:
            continue
        for s_str, e_str in ALLOWED_SLOTS:
            s_dt, e_dt = build_slot_dt(day, (s_str, e_str))
            # 必须在 from_dt 之后
            if e_dt <= from_dt:
                continue
            slots.append((s_dt, e_dt))
    # 自然顺序 = 时间顺序
    return slots

def clear_slot_for_group(
    engine,
    teacher_id: int,
    student_ids: List[int],
    slot_start_dt: datetime,
    slot_end_dt: datetime,
    depth: int,
    max_depth: int,
    horizon_days: int,
) -> Optional[List[Dict[str, Any]]]:
    """
    目标：让 (teacher_id, student_ids) 在 [slot_start_dt, slot_end_dt] 这个时间段空出来。
    可以通过挪走这个时间段里的冲突课程来实现，最多递归 max_depth 层。

    返回：
      - 若成功：返回 move_plan 列表
        每个元素形如：
          {
              "original_lesson": {...},
              "new_start": "YYYY-MM-DD HH:MM:SS",
              "new_end": "YYYY-MM-DD HH:MM:SS",
          }
      - 若在当前 depth~max_depth 范围内做不到：返回 None
    """

    # 1. 找出这个时间段内，teacher 或 student_ids 有哪些课挡路
    conflicts = get_conflicting_lessons_for_group(
        engine,
        teacher_id=teacher_id,
        student_ids=student_ids,
        slot_start_dt=slot_start_dt,
        slot_end_dt=slot_end_dt,
    )
    if not conflicts:
        # 已经没有冲突，不需要挪任何课
        return []

    move_plan: List[Dict[str, Any]] = []

    for les in conflicts:
        class_id = les.get("class_id")
        if class_id is None:
            # 没有 class_id，没法进一步查询参与者，保守失败
            return None

        # 这一节课真正参与的老师 + 学生
        les_teacher_id, les_student_ids = get_class_participants(engine, class_id)

        # 从这节课原时间之后开始找
        les_start_str = les.get("lesson_start")
        try:
            from_dt = datetime.fromisoformat(str(les_start_str))
        except Exception:
            from_dt = slot_start_dt  # 兜底

        # 2. 第一招：尝试一层挪课 —— 找一个所有参与者都空闲的未来 slot
        new_start_dt, new_end_dt = find_future_slot_for_class_discrete(
            engine,
            teacher_id=les_teacher_id,
            student_ids=les_student_ids,
            from_dt=from_dt,
            horizon_days=horizon_days,
        )
        if new_start_dt is not None:
            # 很好，这节课不用牵连别人就能挪走
            move_plan.append({
                "original_lesson": les,
                "new_start": new_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "new_end": new_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            })
            continue

        # 3. 一层挪课失败，如果已经到最大 depth，就整体失败
        if depth >= max_depth:
            return None

        # 4. 第二招：在未来一周枚举一些候选时间段，把这节课挪过去，但需要先"清空"那个时间段
        candidate_slots = enumerate_future_slots(from_dt=from_dt, horizon_days=horizon_days)

        success_for_this_lesson = False

        for cand_start_dt, cand_end_dt in candidate_slots:
            # 4.1 递归尝试：清空 cand_slot
            sub_plan = clear_slot_for_group(
                engine,
                teacher_id=les_teacher_id,
                student_ids=les_student_ids,
                slot_start_dt=cand_start_dt,
                slot_end_dt=cand_end_dt,
                depth=depth + 1,
                max_depth=max_depth,
                horizon_days=horizon_days,
            )
            if sub_plan is None:
                # 这个候选时间段没法在给定 depth 范围内清空，换下一个
                continue

            # 4.2 cand_slot 已经可以被清空，把当前这节课挪过去
            move_plan.append({
                "original_lesson": les,
                "new_start": cand_start_dt.strftime("%Y-%m-%d %H:%M:%S"),
                "new_end": cand_end_dt.strftime("%Y-%m-%d %H:%M:%S"),
            })
            # 把清空 cand_slot 过程中产生的挪课计划也加进来
            move_plan.extend(sub_plan)

            success_for_this_lesson = True
            break

        if not success_for_this_lesson:
            # 所有候选时间都清不出位置给这节课
            return None

    # 所有挡路的课都成功移走了
    return move_plan

def extract_conflicting_lessons(check_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    从 check_schedule 的返回中，提取老师在该时间段的所有lesson。
    这些就是"候选可以被挪走的课"。
    """
    return check_result.get("teacher_struct", [])

def find_future_slots_for_class_discrete_all(
    engine,
    teacher_id: int,
    student_ids: List[int],
    from_dt: datetime,
    horizon_days: int = 7,
    max_slots: int = 20,
) -> List[Tuple[datetime, datetime]]:
    """
    在 from_dt 之后 horizon_days 的范围内，寻找所有"老师 + 学生都空闲"的 90min slot。
    - slot 固定为 ALLOWED_SLOTS 中的时间段
    - 只考虑工作日（周一~周五），不排周末
    - 最多返回 max_slots 个候选（以免结果太长）
    """
    results: List[Tuple[datetime, datetime]] = []

    for day_offset in range(horizon_days):
        day = (from_dt + timedelta(days=day_offset)).date()

        # weekday(): Monday=0, Sunday=6；5、6 是周六周日 → 跳过
        if day.weekday() >= 5:
            continue

        for slot in ALLOWED_SLOTS:
            slot_start_dt, slot_end_dt = build_slot_dt(day, slot)

            # 必须在 from_dt 之后
            if slot_end_dt <= from_dt:
                continue

            start_unix = int(slot_start_dt.timestamp())
            end_unix = int(slot_end_dt.timestamp())

            if is_group_free(engine, teacher_id, student_ids, start_unix, end_unix):
                results.append((slot_start_dt, slot_end_dt))
                if len(results) >= max_slots:
                    return results
    return results

def move_existing_lessons_strategy(
    engine,
    client,
    model: str,
    student_name: str,
    teacher_name: str,
    intent_start: str,
    intent_end: str,
    base_check_result: Dict[str, Any],
) -> Dict[str, Any]:
    """
    方案二：考虑把老师在这段时间的冲突课挪到未来某个共同空档。
    目标：只需要在 intent_start ~ intent_end 之间空出"一个完整的 90 分钟 slot"，
    而不是把所有冲突课全搬走。

    这里不再生成 LLM 文本说明，只返回结构化的候选方案：
    {
        "success": True/False,
        "candidates": [
            {
                "slot": "YYYY-MM-DD HH:MM:SS ~ YYYY-MM-DD HH:MM:SS",  # 目标新课时间
                "move_plan": [
                    {
                        "original_lesson": {...},   # 原课程记录
                        "options": [               # 这节课所有可选的新时间
                            {"new_start": "...", "new_end": "..."},
                            ...
                        ],
                    },
                    ...
                ],
            },
            ...
        ],
        "reason": "如果 success=False，会给出原因（可选）",
    }
    """
    # 1. 抽出老师在原始意向时间段内的所有课程（冲突课）
    conflicts = extract_conflicting_lessons(base_check_result)
    if not conflicts:
        return {
            "success": False,
            "candidates": [],
            "reason": "该老师在目标时间段没有任何课程记录，理论上不必挪课，但仍有冲突，可能是数据问题。",
        }

    # 2. 解析意向时间
    intent_start_dt = datetime.fromisoformat(intent_start)
    intent_end_dt = datetime.fromisoformat(intent_end)
    target_date = intent_start_dt.date()  # 假设新课只安排在同一天

    # 3. 枚举目标时间段内的所有"可用 slot"（90min固定节次）
    candidate_target_slots: List[Tuple[datetime, datetime]] = []
    for slot in ALLOWED_SLOTS:
        slot_start_dt, slot_end_dt = build_slot_dt(target_date, slot)
        # 只保留完全落在意向时间区间内的 slot
        if slot_start_dt >= intent_start_dt and slot_end_dt <= intent_end_dt:
            candidate_target_slots.append((slot_start_dt, slot_end_dt))

    if not candidate_target_slots:
        return {
            "success": False,
            "candidates": [],
            "reason": f"在 {intent_start} ~ {intent_end} 区间内没有完整的 90 分钟固定上课时间槽，无法安排新课。",
        }

    # 4. 针对每一个候选目标 slot：尝试给所有冲突课找【未来一周内所有可行新时间】
    all_possible_move_plans: List[Dict[str, Any]] = []

    for slot_start_dt, slot_end_dt in candidate_target_slots:
        # 4.1 找出"与这个 slot 有重叠"的课程（只挪这些）
        slot_conflicts: List[Dict[str, Any]] = []
        for les in conflicts:
            les_start_str = les.get("lesson_start")
            les_end_str = les.get("lesson_end")
            try:
                les_start_dt = datetime.fromisoformat(str(les_start_str))
                les_end_dt = datetime.fromisoformat(str(les_end_str))
            except Exception:
                continue

            # 判断时间段是否重叠
            if intervals_overlap(les_start_dt, les_end_dt, slot_start_dt, slot_end_dt):
                slot_conflicts.append(les)

        # 这个 slot 对老师来说如果本来就没有冲突课，那 move_plan 为空也算一个合法方案
        if not slot_conflicts:
            all_possible_move_plans.append({
                "slot": f"{slot_start_dt.strftime('%Y-%m-%d %H:%M:%S')} ~ {slot_end_dt.strftime('%Y-%m-%d %H:%M:%S')}",
                "move_plan": [],
            })
            continue

        # 4.2 对这些与 slot 冲突的课逐个尝试找未来一周内所有可行 slot
        current_move_plan: List[Dict[str, Any]] = []
        can_free_this_slot = True

        for les in slot_conflicts:
            class_id = les.get("class_id")
            if class_id is None:
                # 没有 class_id，没法继续，保守认为这个 slot 不可用
                can_free_this_slot = False
                break

            teacher_id, student_ids = get_class_participants(engine, class_id)

            # 从这节课的原时间之后开始找未来 slot
            les_start_str = les.get("lesson_start")
            try:
                from_dt = datetime.fromisoformat(str(les_start_str))
            except Exception:
                from_dt = slot_start_dt  # 兜底：用目标 slot 起始时间

            # ⭐ 拿到"未来一周内所有可行的 slot"（你之前已经实现了这个函数）
            all_slots = find_future_slots_for_class_discrete_all(
                engine,
                teacher_id=teacher_id,
                student_ids=student_ids,
                from_dt=from_dt,
                horizon_days=7,
                max_slots=20,
            )

            if not all_slots:
                # 这一节课在未来一周工作日内找不到任何可行 slot → 这个目标 slot 失败
                can_free_this_slot = False
                break

            options = [
                {
                    "new_start": s.strftime("%Y-%m-%d %H:%M:%S"),
                    "new_end": e.strftime("%Y-%m-%d %H:%M:%S"),
                }
                for (s, e) in all_slots
            ]

            current_move_plan.append({
                "original_lesson": les,
                "options": options,   # 每节课一个 options 列表
            })

        if can_free_this_slot:
            all_possible_move_plans.append({
                "slot": f"{slot_start_dt.strftime('%Y-%m-%d %H:%M:%S')} ~ {slot_end_dt.strftime('%Y-%m-%d %H:%M:%S')}",
                "move_plan": current_move_plan,
            })

    # 5. 汇总返回
    if not all_possible_move_plans:
        return {
            "success": False,
            "candidates": [],
            "reason": "在目标日期的所有固定上课时间槽中，无法在未来一周内为冲突课程找到任何替代时间。",
        }

    return {
        "success": True,
        "candidates": all_possible_move_plans,
        "reason": "",
    }

def summarize_student_week(engine, student_name: str, week_start: str, week_end: str) -> List[Dict[str, Any]]:
    """
    给 LLM 看一个学生一段时间内的完整课表（比如一周）。
    week_start / week_end: 'YYYY-MM-DD 00:00:00' ~ 'YYYY-MM-DD 23:59:59'
    这里只查 student_classes + lessons。
    """
    start_dt = datetime.fromisoformat(week_start)
    end_dt = datetime.fromisoformat(week_end)
    start_unix = int(start_dt.timestamp())
    end_unix = int(end_dt.timestamp())

    _, student_id = get_ids(engine, teacher_name="", student_name=student_name)

    if student_id is None:
        return []

    with engine.connect() as conn:
        sql = text("""
            SELECT l.id                                                            AS lesson_id,
                   IF(l.start_time = -1, 'PERMANENT', FROM_UNIXTIME(l.start_time)) AS lesson_start,
                   IF(l.end_time = -1, 'PERMANENT', FROM_UNIXTIME(l.end_time))     AS lesson_end,
                   s.id                                                            AS subject_id,
                   s.class_id                                                      AS class_id,
                   s.teacher_id                                                    AS teacher_id,
                   c.class_type                                                    AS class_name,
                   t.name                                                          AS topic_name,
                   t.cn_name                                                       AS topic_cn_name
            FROM lessons l
                     JOIN subjects s ON l.subject_id = s.id
                     JOIN classes c ON s.class_id = c.id
                     JOIN student_classes sc ON sc.class_id = s.class_id
                     LEFT JOIN topics t ON s.topic_id = t.id
            WHERE ((l.start_time = -1 OR l.start_time <= :end_unix)
                AND (l.end_time = -1 OR l.end_time >= :start_unix))
              AND ((sc.start_time = -1 OR sc.start_time <= :end_unix)
                AND (sc.end_time = -1 OR sc.end_time >= :start_unix))
              AND sc.student_id = :student_id
            ORDER BY l.start_time
        """)
        rows = conn.execute(
            sql,
            {
                "start_unix": start_unix,
                "end_unix": end_unix,
                "student_id": student_id,
            }
        ).mappings().all()

    result = []
    for r in rows:
        row = dict(r)
        topic_name = row.get("topic_name") or ""
        topic_cn_name = row.get("topic_cn_name") or ""
        row["topic"] = f"{topic_name}{topic_cn_name}"
        result.append(row)
    return result

# =========================================================
# 2a. RescheduleAgent 工具函数（给 agent 用的 tool）
# =========================================================

def tool_check_slot(
    engine,
    teacher_id: int,
    student_id: int,
    date_str: str,
    slot_index: int,
) -> Dict[str, Any]:
    """
    工具1: 查询某个 timeslot 内老师和学生的课表。
    返回结构化的冲突信息。
    """
    return query_lesson_schedule(engine, date_str, slot_index, teacher_id, student_id)


def tool_find_alt_teachers(
    engine,
    topic_ids: List[int],
    student_id: int,
    date_str: str,
    slot_index: int,
) -> List[Dict[str, Any]]:
    """
    工具2: 找到教同一科目的其他老师，并检查他们在指定时间是否有空。
    返回可用老师列表。
    """
    if not topic_ids:
        return []

    slot = ALLOWED_SLOTS[slot_index]
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    slot_start_dt, slot_end_dt = build_slot_dt(target_date, slot)
    start_unix = int(slot_start_dt.timestamp())
    end_unix = int(slot_end_dt.timestamp())

    teacher_ids = fetch_teachers_for_topics(engine, topic_ids)
    teacher_map = fetch_teacher_names(engine, teacher_ids)

    results = []
    for tid, tname in teacher_map.items():
        free = is_group_free(engine, tid, [student_id], start_unix, end_unix)
        results.append({
            "teacher_id": tid,
            "teacher_name": tname,
            "is_free": free,
            "slot": f"{slot_start_dt.strftime('%Y-%m-%d %H:%M')} ~ {slot_end_dt.strftime('%H:%M')}",
        })

    return results


def tool_find_alt_times_for_teacher(
    engine,
    teacher_id: int,
    student_id: int,
    from_date_str: str,
    horizon_days: int = 14,
    max_slots: int = 20,
) -> List[Dict[str, str]]:
    """
    工具3: 同一个老师，找未来 horizon_days 内所有老师+学生都空闲的时间段。
    """
    from_dt = datetime.strptime(from_date_str, "%Y-%m-%d")
    slots = find_future_slots_for_class_discrete_all(
        engine,
        teacher_id=teacher_id,
        student_ids=[student_id],
        from_dt=from_dt,
        horizon_days=horizon_days,
        max_slots=max_slots,
    )
    return [
        {"start": s.strftime("%Y-%m-%d %H:%M:%S"), "end": e.strftime("%Y-%m-%d %H:%M:%S")}
        for s, e in slots
    ]


def tool_try_move_lesson(
    engine,
    lesson_class_id: int,
    target_date_str: str,
    target_slot_index: int,
    horizon_days: int = 14,
    max_depth: int = 2,
) -> Dict[str, Any]:
    """
    工具4: 尝试把某个 class 的课挪开，腾出指定时间段。
    使用递归 clear_slot_for_group。
    返回 move_plan 或 None。
    """
    teacher_id, student_ids = get_class_participants(engine, lesson_class_id)
    if teacher_id is None:
        return {"success": False, "reason": f"class_id={lesson_class_id} 找不到 teacher_id"}

    slot = ALLOWED_SLOTS[target_slot_index]
    target_date = datetime.strptime(target_date_str, "%Y-%m-%d").date()
    slot_start_dt, slot_end_dt = build_slot_dt(target_date, slot)

    move_plan = clear_slot_for_group(
        engine,
        teacher_id=teacher_id,
        student_ids=student_ids,
        slot_start_dt=slot_start_dt,
        slot_end_dt=slot_end_dt,
        depth=0,
        max_depth=max_depth,
        horizon_days=horizon_days,
    )

    if move_plan is None:
        return {"success": False, "reason": "在给定深度和时间范围内无法挪课"}

    return {
        "success": True,
        "move_plan": move_plan,
        "freed_slot": f"{slot_start_dt.strftime('%Y-%m-%d %H:%M:%S')} ~ {slot_end_dt.strftime('%Y-%m-%d %H:%M:%S')}",
    }


def tool_find_student_movable_lessons(
    engine,
    student_id: int,
    date_str: str,
    slot_index: int,
    horizon_days: int = 14,
) -> List[Dict[str, Any]]:
    """
    工具5: 查看学生在指定时间段有什么课，并为每节课找到可选的替代时间。
    用于"挪掉学生自己的课"策略。
    """
    slot = ALLOWED_SLOTS[slot_index]
    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    slot_start_dt, slot_end_dt = build_slot_dt(target_date, slot)
    start_unix = int(slot_start_dt.timestamp())
    end_unix = int(slot_end_dt.timestamp())

    # 找出学生在该时间段的课
    with engine.connect() as conn:
        sql = text("""
            SELECT
                l.id AS lesson_id,
                IF(l.start_time=-1,'PERMANENT',FROM_UNIXTIME(l.start_time)) AS lesson_start,
                IF(l.end_time=-1,'PERMANENT',FROM_UNIXTIME(l.end_time)) AS lesson_end,
                s.id AS subject_id, s.class_id, s.teacher_id,
                c.class_type AS class_name,
                t.name AS topic_name, t.cn_name AS topic_cn_name
            FROM lessons l
            JOIN subjects s ON l.subject_id = s.id
            JOIN classes c ON s.class_id = c.id
            JOIN student_classes sc ON sc.class_id = s.class_id
            LEFT JOIN topics t ON s.topic_id = t.id
            WHERE ((l.start_time=-1 OR l.start_time <= :end_unix)
               AND (l.end_time=-1 OR l.end_time >= :start_unix))
              AND ((sc.start_time=-1 OR sc.start_time <= :end_unix)
               AND (sc.end_time=-1 OR sc.end_time >= :start_unix))
              AND sc.student_id = :sid
            ORDER BY l.start_time
        """)
        rows = conn.execute(
            sql, {"start_unix": start_unix, "end_unix": end_unix, "sid": student_id}
        ).mappings().all()

    results = []
    seen = set()
    for r in rows:
        row = dict(r)
        lid = row["lesson_id"]
        if lid in seen:
            continue
        seen.add(lid)

        class_id = row["class_id"]
        les_teacher_id, les_student_ids = get_class_participants(engine, class_id)

        # 找这门课的所有可选替代时间
        alt_slots = find_future_slots_for_class_discrete_all(
            engine,
            teacher_id=les_teacher_id or 0,
            student_ids=les_student_ids,
            from_dt=slot_start_dt,
            horizon_days=horizon_days,
            max_slots=10,
        )

        row["topic"] = (row.get("topic_name") or "") + (row.get("topic_cn_name") or "")
        row["alternative_times"] = [
            {"start": s.strftime("%Y-%m-%d %H:%M:%S"), "end": e.strftime("%Y-%m-%d %H:%M:%S")}
            for s, e in alt_slots
        ]
        results.append(row)

    return results


def tool_get_topic_ids_for_subject(engine, class_id: int) -> List[int]:
    """
    工具6: 根据 class_id 查出对应的 topic_id 列表。
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT DISTINCT topic_id FROM subjects WHERE class_id = :cid AND topic_id IS NOT NULL"),
            {"cid": class_id},
        ).fetchall()
    return [r[0] for r in rows]


# =========================================================
# 2b. RescheduleAgent — 多策略、自主探索的换课 Agent
# =========================================================

# Agent 可调用的工具注册表
AGENT_TOOLS = [
    {
        "type": "function",
        "name": "check_slot",
        "description": "查看指定日期、时间段内老师和学生各有什么课。slot_index: 0=09:00-10:30, 1=10:30-12:00, 2=13:30-15:00, 3=15:00-16:30, 4=16:30-18:00, 5=18:00-19:30",
        "parameters": {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                "slot_index": {"type": "integer", "description": "时间段索引 0-5"},
                "teacher_id": {"type": "integer", "description": "老师ID"},
                "student_id": {"type": "integer", "description": "学生ID"},
            },
            "required": ["date", "slot_index", "teacher_id", "student_id"],
        },
    },
    {
        "type": "function",
        "name": "find_alt_teachers",
        "description": "找到教同一科目(topic)的所有老师，并检查他们在指定时间段是否有空。需要先知道 topic_ids。",
        "parameters": {
            "type": "object",
            "properties": {
                "topic_ids": {"type": "array", "items": {"type": "integer"}, "description": "科目ID列表"},
                "student_id": {"type": "integer", "description": "学生ID"},
                "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                "slot_index": {"type": "integer", "description": "时间段索引 0-5"},
            },
            "required": ["topic_ids", "student_id", "date", "slot_index"],
        },
    },
    {
        "type": "function",
        "name": "find_alt_times",
        "description": "为指定的老师和学生，在未来 N 天内找所有双方都空闲的 90 分钟时间段。",
        "parameters": {
            "type": "object",
            "properties": {
                "teacher_id": {"type": "integer", "description": "老师ID"},
                "student_id": {"type": "integer", "description": "学生ID"},
                "from_date": {"type": "string", "description": "起始日期 YYYY-MM-DD"},
                "horizon_days": {"type": "integer", "description": "搜索范围天数，默认14", "default": 14},
            },
            "required": ["teacher_id", "student_id", "from_date"],
        },
    },
    {
        "type": "function",
        "name": "try_move_lesson",
        "description": "尝试把某个 class 在指定时间段的课挪走（递归挪课）。返回挪课方案或失败原因。",
        "parameters": {
            "type": "object",
            "properties": {
                "class_id": {"type": "integer", "description": "要挪课的 class_id"},
                "target_date": {"type": "string", "description": "目标日期 YYYY-MM-DD"},
                "target_slot_index": {"type": "integer", "description": "目标时间段索引 0-5"},
                "max_depth": {"type": "integer", "description": "递归深度，默认2", "default": 2},
            },
            "required": ["class_id", "target_date", "target_slot_index"],
        },
    },
    {
        "type": "function",
        "name": "find_student_movable",
        "description": "查看学生在指定时间段有什么课，并为每节课找到可选的替代时间。用于考虑挪学生自己的课。",
        "parameters": {
            "type": "object",
            "properties": {
                "student_id": {"type": "integer", "description": "学生ID"},
                "date": {"type": "string", "description": "日期 YYYY-MM-DD"},
                "slot_index": {"type": "integer", "description": "时间段索引 0-5"},
            },
            "required": ["student_id", "date", "slot_index"],
        },
    },
    {
        "type": "function",
        "name": "get_topic_ids",
        "description": "根据 class_id 查出对应的科目 topic_id 列表。用于知道一门课属于什么科目，以便找替代老师。",
        "parameters": {
            "type": "object",
            "properties": {
                "class_id": {"type": "integer", "description": "课程 class_id"},
            },
            "required": ["class_id"],
        },
    },
    {
        "type": "function",
        "name": "propose_plan",
        "description": "提交一个完整的换课方案。可以提交多个方案（每次调用提交一个），系统会收集所有方案供管理员选择。",
        "parameters": {
            "type": "object",
            "properties": {
                "plan_name": {"type": "string", "description": "方案名称，如 '方案A：换老师'"},
                "disruption_level": {"type": "string", "enum": ["low", "medium", "high"], "description": "对现有课表的影响程度"},
                "description": {"type": "string", "description": "方案说明（中文）"},
                "steps": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "具体操作步骤列表",
                },
                "affected_people": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "受影响的老师/学生名单",
                },
            },
            "required": ["plan_name", "disruption_level", "description", "steps"],
        },
    },
    {
        "type": "function",
        "name": "finish",
        "description": "当你已经探索了足够多的策略并提交了多个方案后，调用此工具结束。给出最终总结。",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "最终总结（中文），说明你尝试了哪些策略，推荐哪个方案"},
            },
            "required": ["summary"],
        },
    },
]


class RescheduleAgent:
    """
    多策略自主换课 Agent。

    核心思路（按干扰程度从低到高）：
    策略1: 直接排课 — 如果时间和老师都有空，直接排
    策略2: 换老师 — 同科目其他有空的老师
    策略3: 换时间 — 同一老师，不同时间段
    策略4: 挪别人的课 — 把占位的课挪到别的时间
    策略5: 挪自己的课 — 把学生自己的课挪走再排新课

    Agent 会自主尝试多种组合，并生成多个方案供管理员选择。
    """

    def __init__(self, engine, client: OpenAI, model: str = "gpt-4.1"):
        self.engine = engine
        self.client = client
        self.model = model
        self.proposals: List[Dict[str, Any]] = []

    def _execute_tool(self, tool_name: str, args: Dict[str, Any]) -> Any:
        """根据 tool_name 分发到对应的 Python 函数。"""
        if tool_name == "check_slot":
            return tool_check_slot(
                self.engine,
                teacher_id=args["teacher_id"],
                student_id=args["student_id"],
                date_str=args["date"],
                slot_index=args["slot_index"],
            )
        elif tool_name == "find_alt_teachers":
            return tool_find_alt_teachers(
                self.engine,
                topic_ids=args["topic_ids"],
                student_id=args["student_id"],
                date_str=args["date"],
                slot_index=args["slot_index"],
            )
        elif tool_name == "find_alt_times":
            return tool_find_alt_times_for_teacher(
                self.engine,
                teacher_id=args["teacher_id"],
                student_id=args["student_id"],
                from_date_str=args["from_date"],
                horizon_days=args.get("horizon_days", 14),
            )
        elif tool_name == "try_move_lesson":
            return tool_try_move_lesson(
                self.engine,
                lesson_class_id=args["class_id"],
                target_date_str=args["target_date"],
                target_slot_index=args["target_slot_index"],
                max_depth=args.get("max_depth", 2),
            )
        elif tool_name == "find_student_movable":
            return tool_find_student_movable_lessons(
                self.engine,
                student_id=args["student_id"],
                date_str=args["date"],
                slot_index=args["slot_index"],
            )
        elif tool_name == "get_topic_ids":
            return tool_get_topic_ids_for_subject(self.engine, class_id=args["class_id"])
        elif tool_name == "propose_plan":
            proposal = {
                "plan_name": args["plan_name"],
                "disruption_level": args["disruption_level"],
                "description": args["description"],
                "steps": args.get("steps", []),
                "affected_people": args.get("affected_people", []),
            }
            self.proposals.append(proposal)
            return {"status": "ok", "proposal_index": len(self.proposals) - 1}
        elif tool_name == "finish":
            return {"status": "finished", "summary": args["summary"]}
        else:
            return {"error": f"未知工具: {tool_name}"}

    def _build_system_prompt(
        self,
        student_name: str,
        teacher_name: str,
        student_id: int,
        teacher_id: int,
        requirement: str,
        date_str: str,
        slot_index: int,
    ) -> str:
        slot = ALLOWED_SLOTS[slot_index]
        return f"""你是一个智能排课 Agent，负责帮助管理员完成换课/排课任务。

## 任务背景
- 学生: {student_name} (ID: {student_id})
- 意向老师: {teacher_name} (ID: {teacher_id})
- 意向时间: {date_str} {slot[0]}~{slot[1]} (slot_index={slot_index})
- 排课需求: {requirement}

## 时间段索引对照表
0 = 09:00-10:30 | 1 = 10:30-12:00 | 2 = 13:30-15:00
3 = 15:00-16:30 | 4 = 16:30-18:00 | 5 = 18:00-19:30
只有工作日（周一到周五），没有周末。每节课 90 分钟。

## 你的策略（按干扰程度从低到高尝试）

**策略1: 直接排课**
先用 check_slot 看看意向时间老师和学生是否都有空。如果都有空，直接 propose 一个方案。

**策略2: 换老师**
如果意向老师忙，先用 get_topic_ids 查出冲突课的科目，再用 find_alt_teachers 找同科目的其他空闲老师。如果找到了，propose 一个换老师方案。

**策略3: 换时间**
如果不想换老师，用 find_alt_times 为同一个老师找其他双方都空闲的时间。尽量找最近的、对学生影响最小的时间段。propose 一个换时间方案。

**策略4: 挪别人的课**
如果以上都不行，看看是谁占了那个时间段（用 check_slot 的返回数据），用 try_move_lesson 尝试把别人的课挪走。propose 一个挪别人课的方案。

**策略5: 挪自己的课**
如果学生自己在那个时间段也有课，用 find_student_movable 看看学生的课能不能挪到别的时间。propose 一个方案。

## 工作规则
1. 你必须**主动、多次**地使用工具去探索各种可能性，不要只试一种就放弃。
2. 你应该尽量生成 **2-4 个不同方案**（propose_plan），让管理员有选择余地。
3. 每个方案要标明 disruption_level (low/medium/high)。
4. 最终调用 finish 给出总结和推荐。
5. 如果所有策略都试过仍然无解，也要在 finish 中说明原因。
6. 不要在一步内就直接 finish，要先充分探索。
7. 在给出 propose_plan 中，所有时间要写成具体的 "YYYY-MM-DD HH:MM ~ HH:MM" 格式。
8. 用中文回复。"""

    def run(
        self,
        student_name: str,
        teacher_name: str,
        student_id: int,
        teacher_id: int,
        requirement: str,
        date_str: str,
        slot_index: int,
        max_steps: int = 12,
    ) -> Dict[str, Any]:
        """
        运行 Agent，返回:
        {
            "proposals": [...],   # 所有收集到的方案
            "summary": "...",     # 最终总结
            "steps_used": int,    # 使用了多少步
        }
        """
        self.proposals = []

        system_prompt = self._build_system_prompt(
            student_name, teacher_name, student_id, teacher_id,
            requirement, date_str, slot_index,
        )

        # 构建 messages（OpenAI Responses API 用 input 列表）
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "请开始排课分析。先检查意向时间的冲突情况，然后按策略优先级逐步尝试。"},
        ]

        for step in range(max_steps):
            print(f"  [Agent 第 {step+1} 步]")

            resp = self.client.responses.create(
                model=self.model,
                input=messages,
                tools=AGENT_TOOLS,
            )

            # 处理 response
            assistant_msg = {"role": "assistant", "content": []}
            tool_calls_to_process = []

            for item in resp.output:
                if hasattr(item, "type"):
                    if item.type == "function_call":
                        tool_calls_to_process.append(item)
                        assistant_msg["content"].append({
                            "type": "function_call",
                            "id": item.id,
                            "call_id": item.call_id,
                            "name": item.name,
                            "arguments": item.arguments,
                        })
                    elif item.type == "message" or hasattr(item, "content"):
                        # 文本回复
                        if hasattr(item, "content"):
                            for c in item.content:
                                if hasattr(c, "text"):
                                    assistant_msg["content"].append({
                                        "type": "text",
                                        "text": c.text,
                                    })
                                    print(f"    Agent: {c.text[:200]}...")

            messages.append(assistant_msg)

            if not tool_calls_to_process:
                # 没有工具调用，可能是纯文本回复或结束
                break

            # 执行所有工具调用
            finished = False
            for tc in tool_calls_to_process:
                func_name = tc.name
                try:
                    func_args = json.loads(tc.arguments)
                except json.JSONDecodeError:
                    func_args = {}

                print(f"    -> 调用 {func_name}({json.dumps(func_args, ensure_ascii=False)[:100]})")
                tool_result = self._execute_tool(func_name, func_args)

                # 序列化工具结果
                result_str = json.dumps(tool_result, ensure_ascii=False, default=str)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.call_id,
                    "content": result_str,
                })

                if func_name == "finish":
                    finished = True

            if finished:
                return {
                    "proposals": self.proposals,
                    "summary": tool_result.get("summary", ""),
                    "steps_used": step + 1,
                }

        # 超过 max_steps 还没 finish，强制总结
        summary = self._force_summary(messages)
        return {
            "proposals": self.proposals,
            "summary": summary,
            "steps_used": max_steps,
        }

    def _force_summary(self, messages: List) -> str:
        """超步数后强制让 LLM 给出最终总结。"""
        messages.append({
            "role": "user",
            "content": "你已经用完了所有步数。请立即调用 finish 工具，给出最终总结，包括你已经找到的所有方案和推荐。",
        })
        resp = self.client.responses.create(
            model=self.model,
            input=messages,
            tools=AGENT_TOOLS,
        )
        for item in resp.output:
            if hasattr(item, "type") and item.type == "function_call" and item.name == "finish":
                try:
                    args = json.loads(item.arguments)
                    return args.get("summary", "Agent 未能给出总结。")
                except Exception:
                    pass
            if hasattr(item, "content"):
                for c in item.content:
                    if hasattr(c, "text"):
                        return c.text
        return "Agent 超步数且未能生成总结。"


# =========================================================
# 2c. Legacy SchedulingAgent (保留兼容)
# =========================================================

class SchedulingAgent:
    """
    一个非常简化的「多轮 agent」：
    - LLM 只输出 JSON 指令
    - Python 根据指令调用工具（check_schedule / summarize_student_week）
    - 状态中记录每轮工具结果，继续喂给下一轮
    """
    def __init__(self, engine, client: OpenAI, model: str = "gpt-4.1"):
        self.engine = engine
        self.client = client
        self.model = model

    def run(
        self,
        student_name: str,
        teacher_name: str,
        requirement: str,
        intent_start: str,
        intent_end: str,
        week_start: str,
        week_end: str,
        max_steps: int = 6,
    ) -> str:
        # 初始化状态
        student_week = summarize_student_week(self.engine, student_name, week_start, week_end)
        state: Dict[str, Any] = {
            "student_name": student_name,
            "teacher_name": teacher_name,
            "requirement": requirement,
            "intent_interval": {
                "start": intent_start,
                "end": intent_end,
            },
            "student_week": student_week,
            "tool_history": [],   # 每一次工具调用的记录
            "plan_candidate": [], # LLM 拟定中的换课方案（可选）
        }

        # 每一轮：把 state 丢给 LLM，请它返回 JSON 指令
        for step in range(max_steps):
            llm_json = self._call_llm(state)
            action = llm_json.get("action")

            if action == "finish":
                # LLM 认为已经给出了最终方案
                final_text = llm_json.get("final_text", "（LLM 未提供 final_text）")
                return final_text

            elif action == "check_interval":
                # 让算法去查具体某个时间段的 teacher / student 冲突情况
                interval = llm_json.get("interval", {})
                start = interval.get("start")
                end = interval.get("end")
                teacher = interval.get("teacher_name", teacher_name)
                student = interval.get("student_name", student_name)

                tool_result = check_schedule(
                    self.engine,
                    start_time_str=start,
                    end_time_str=end,
                    teacher_name=teacher,
                    student_name=student,
                )
                state["tool_history"].append({
                    "tool": "check_schedule",
                    "input": interval,
                    "output": tool_result,
                })

            elif action == "clear_slot_for_group":
                # 让算法尝试通过挪课的方式，清空某个时间段
                slot = llm_json.get("slot", {})
                slot_start = slot.get("start")
                slot_end = slot.get("end")
                max_depth = llm_json.get("max_depth", 1)
                horizon_days = llm_json.get("horizon_days", 7)

                try:
                    slot_start_dt = datetime.fromisoformat(slot_start)
                    slot_end_dt = datetime.fromisoformat(slot_end)
                except Exception:
                    state["tool_history"].append({
                        "tool": "clear_slot_for_group",
                        "input": slot,
                        "output": "invalid datetime format",
                    })
                else:
                    # 目前简单地认为 group = 这位老师 + 这位学生
                    teacher_id, student_id = get_ids(self.engine, teacher_name, student_name)
                    if teacher_id is None or student_id is None:
                        move_plan = None
                    else:
                        move_plan = clear_slot_for_group(
                            self.engine,
                            teacher_id=teacher_id,
                            student_ids=[student_id],
                            slot_start_dt=slot_start_dt,
                            slot_end_dt=slot_end_dt,
                            depth=0,
                            max_depth=max_depth,
                            horizon_days=horizon_days,
                        )

                    state["tool_history"].append({
                        "tool": "clear_slot_for_group",
                        "input": {
                            "slot": slot,
                            "max_depth": max_depth,
                            "horizon_days": horizon_days,
                        },
                        "output": move_plan,
                    })

            elif action == "update_plan":
                # LLM 给出一个中间的换课方案，我们只是记录下来
                plan = llm_json.get("plan", {})
                state["plan_candidate"].append(plan)

            else:
                # 未知 action，直接记录错误信息，下一轮让 LLM 自己修正
                state["tool_history"].append({
                    "tool": "error",
                    "input": llm_json,
                    "output": "Unknown action",
                })


        # 超过 max_steps 还没 finish，就让 LLM 做最后总结
        summary = self._force_final_summary(state)
        return summary

    def _call_llm(self, state: Dict[str, Any]) -> Dict[str, Any]:
        """
        与 LLM 交互：把当前 state 描述给它，要求只输出 JSON。
        JSON 协议（示例）：

        {
          "action": "check_interval",
          "interval": {
            "start": "2025-11-13 10:30:00",
            "end": "2025-11-13 12:00:00",
            "teacher_name": "xxx",
            "student_name": "yyy"
          }
        }

        或者：
        {
          "action": "update_plan",
          "plan": {
            "description": "先把数学课从周二晚上挪到周四晚上，再把新课放在周二晚上。",
            "steps": [...]
          }
        }

        或者：
        {
          "action": "finish",
          "final_text": "（用中文给管理员的详细排课建议）"
        }
        """
        prompt = f"""
        你是一个负责处理「连续换班 / 换课」问题的排课 AI agent。

        当前上下文 state 如下（JSON）：
        {json.dumps(state, ensure_ascii=False, indent=2)}

        任务说明：
        ...
        你可以做三种事情之一（用 JSON 表达）：
        1) 当你想检查某个时间段的老师/学生是否有课时：
           {{
             "action": "check_interval",
             "interval": {{
               "start": "2025-11-13 10:30:00",
               "end": "2025-11-13 12:00:00",
               "teacher_name": "xxx",
               "student_name": "yyy"
             }}
           }}

        2) 当你想尝试通过挪课清空某个时间段时（可以挪一次或多次）：
           {{
             "action": "clear_slot_for_group",
             "slot": {{
               "start": "2025-11-13 10:30:00",
               "end": "2025-11-13 12:00:00"
             }},
             "max_depth": 1,
             "horizon_days": 7
           }}
           说明：
           - max_depth=1 表示「最多挪一次课」；>1 表示可以连续挪多节课形成链条。
           - horizon_days 表示在未来多少天内为被挪走的课寻找新时间。

        3) 当你想给出一个中间的排课方案草稿时：
           {{
             "action": "update_plan",
             "plan": {{
               "description": "一句中文说明你的整体思路。",
               "steps": [
                 "先把 XX 课从 时间A 换到 时间B",
                 "再把 YY 课从 时间C 换到 时间D",
                 "最后在 时间E 安排新课 ZZ"
               ]
             }}
           }}

        4) 当你觉得已经完整考虑过冲突，找到了一个比较合理的整体方案时：
           {{
             "action": "finish",
             "final_text": "用中文面向排课管理员，详细说明最终建议。包括：哪些课要先删/换；换到什么时候；最后新课安排在什么时候；如果无法满足需求，也要说明原因。"
           }}

        请严格注意：
        ...
        """

        resp = self.client.responses.create(
            model=self.model,
            input=prompt,
        )

        text_out = resp.output[0].content[0].text.strip()
        try:
            parsed = json.loads(text_out)
        except Exception:
            # 如果 LLM 输出坏掉，直接返回 finish，让下一轮人工看
            return {
                "action": "finish",
                "final_text": f"LLM 输出不是合法 JSON，原始输出如下：\n{text_out}"
            }
        return parsed

    def _force_final_summary(self, state: Dict[str, Any]) -> str:
        """
        超过 max_steps 仍未 finish 的 fallback：
        再调用一次 LLM，请它直接写最终总结（不再走 JSON）。
        """
        prompt = f"""
你是一个排课 AI 助手。下面是完整的调度 state（JSON）：

{json.dumps(state, ensure_ascii=False, indent=2)}

你已经进行了多轮工具调用，但还没有给出最终方案。
现在请你直接用中文写出一个「最终排课建议」给人类排课管理员，要求：
- 说明是否有办法通过连续换班（多门课程挪动）来满足需求；
- 如果有，请按步骤列出：哪门课从什么时间换到什么时间，新课安排在哪里；
- 如果没有，请清晰说明冲突点为什么无法解决，并给出备选建议。
"""
        resp = self.client.responses.create(
            model=self.model,
            input=prompt,
        )
        return resp.output[0].content[0].text.strip()

# =========================
# 3. 命令行入口
# =========================

SLOT_DISPLAY = [
    "0: 09:00-10:30",
    "1: 10:30-12:00",
    "2: 13:30-15:00",
    "3: 15:00-16:30",
    "4: 16:30-18:00",
    "5: 18:00-19:30",
]


def print_schedule_result(result: Dict[str, Any]):
    """美观打印 query_lesson_schedule 的返回结果。"""
    print(f"\n  时间段: {result['slot_start']} ~ {result['slot_end']}")

    print(f"\n  【学生课表】 {'有课' if result['student_has_class'] else '无课'}")
    for les in result["student_lessons"]:
        print(f"    - {les.get('topic', '未知课程')} | "
              f"老师: {les.get('teacher_name', '未知')} | "
              f"class_id: {les.get('class_id')}")

    print(f"\n  【老师课表】 {'有课' if result['teacher_has_class'] else '无课'}")
    for les in result["teacher_lessons"]:
        students = ", ".join(
            s["student_name"] or str(s["student_id"])
            for s in les.get("students_in_class", [])
        )
        print(f"    - {les.get('topic', '未知课程')} | "
              f"class: {les.get('class_name', '')} | "
              f"学生: [{students}]")


def print_proposals(proposals: List[Dict[str, Any]]):
    """美观打印 Agent 生成的所有方案。"""
    if not proposals:
        print("\n  Agent 未能生成任何方案。")
        return

    # 按干扰程度排序
    level_order = {"low": 0, "medium": 1, "high": 2}
    sorted_proposals = sorted(
        proposals,
        key=lambda p: level_order.get(p.get("disruption_level", "high"), 3),
    )

    for i, p in enumerate(sorted_proposals, 1):
        level = p.get("disruption_level", "?")
        level_emoji = {"low": "[低影响]", "medium": "[中影响]", "high": "[高影响]"}.get(level, f"[{level}]")

        print(f"\n  ---- {p['plan_name']} {level_emoji} ----")
        print(f"  说明: {p['description']}")
        if p.get("steps"):
            print("  步骤:")
            for j, step in enumerate(p["steps"], 1):
                print(f"    {j}. {step}")
        if p.get("affected_people"):
            print(f"  受影响人员: {', '.join(p['affected_people'])}")


def main():
    client = OpenAI(api_key="")
    llm_model = "gpt-4.1"

    # ===== 一次性输入 =====
    student_name = input("请输入学生名字: ").strip()
    teacher_name = input("请输入意向老师名字: ").strip()
    date_str = input("请输入日期 (YYYY-MM-DD): ").strip()

    print("\n可选时间段:")
    for s in SLOT_DISPLAY:
        print(f"  {s}")
    slot_index = int(input("请选择时间段编号 (0-5): ").strip())

    requirement = input("排课需求（例如：加数学课 / 希望换老师 / 换时间等）: ").strip()

    # ===== 第一步：解析 ID =====
    teacher_id, student_id = get_ids(engine, teacher_name, student_name)
    if teacher_id is None:
        print(f"\n错误：找不到老师 '{teacher_name}'")
        return
    if student_id is None:
        print(f"\n错误：找不到学生 '{student_name}'")
        return

    print(f"\n已识别: 学生 {student_name}(ID:{student_id}), 老师 {teacher_name}(ID:{teacher_id})")

    # ===== 第二步：查询当前时间段课表 =====
    print("\n================ 当前课表查询 ================")
    schedule = query_lesson_schedule(engine, date_str, slot_index, teacher_id, student_id)

    if "error" in schedule:
        print(f"查询错误: {schedule['error']}")
        return

    print_schedule_result(schedule)

    # ===== 第三步：判断是否有冲突 =====
    student_busy = schedule["student_has_class"]
    teacher_busy = schedule["teacher_has_class"]

    if not student_busy and not teacher_busy:
        print("\n================ 排课建议 ================")
        print(f"学生和老师在该时间段均无课，可以直接安排新课。")
        print(f"  时间: {schedule['slot_start']} ~ {schedule['slot_end']}")
        print(f"  老师: {teacher_name}")
        print(f"  学生: {student_name}")
        return

    # 有冲突，显示冲突概况
    print("\n================ 检测到冲突 ================")
    if teacher_busy and student_busy:
        print("  状态: 老师和学生都有课")
    elif teacher_busy:
        print("  状态: 老师有课，学生空闲")
    else:
        print("  状态: 学生有课，老师空闲")

    # ===== 第四步：启动 RescheduleAgent =====
    print("\n================ 启动智能换课 Agent ================")
    print("Agent 将按以下策略自动探索换课方案:")
    print("  1. 换老师 (同科目其他老师)")
    print("  2. 换时间 (同老师不同时间)")
    print("  3. 挪别人的课 (腾出时间段)")
    print("  4. 挪自己的课 (为新课让路)")
    print()

    agent = RescheduleAgent(engine, client, model=llm_model)
    result = agent.run(
        student_name=student_name,
        teacher_name=teacher_name,
        student_id=student_id,
        teacher_id=teacher_id,
        requirement=requirement,
        date_str=date_str,
        slot_index=slot_index,
        max_steps=12,
    )

    # ===== 第五步：输出结果 =====
    print("\n================ Agent 方案汇总 ================")
    print(f"  探索步数: {result['steps_used']}")
    print(f"  生成方案数: {len(result['proposals'])}")

    print_proposals(result["proposals"])

    print(f"\n================ Agent 总结 ================")
    print(f"  {result['summary']}")

    print("\n请管理员从以上方案中选择最终排课方式。\n")


# ===== 旧版入口（保留兼容） =====
def main_legacy():
    """旧版 main()，使用 intent_start/intent_end 输入方式和 SchedulingAgent。"""
    client = OpenAI(api_key="")
    llm_model = "gpt-4.1"

    student_name = input("请输入需要换课/加课/删课的学生名字：").strip()
    teacher_name = input("请输入意向老师名字：").strip()
    intent_start = input("请输入意向开始时间，格式20xx-xx-xx xx:xx:00：").strip()
    intent_end = input("请输入意向结束时间，格式20xx-xx-xx xx:xx:00：").strip()
    requirement = input("申请要求：").strip()

    base = direct_check_and_plan(
        engine,
        student_name=student_name,
        teacher_name=teacher_name,
        intent_start=intent_start,
        intent_end=intent_end,
    )

    status = base["status"]
    check_result = base["check_result"]

    if status == "ok":
        print(f"\n学生和老师在 {intent_start} ~ {intent_end} 均无课程冲突，可以直接排课。")
        return

    # 换老师
    if "teacher_busy" in status:
        change_res = change_teacher_strategy(
            engine, client, llm_model,
            student_name=student_name,
            requirement=requirement,
            intent_start=intent_start,
            intent_end=intent_end,
        )
        print("========== 方案一：换老师 ==========")
        if change_res["success"]:
            for item in change_res["candidates"]:
                print(f"  - {item['teacher_name']} (ID {item['teacher_id']})")
        else:
            print("  没有可用的替代老师")

    # 挪课
    if "teacher_busy" in status:
        move_res = move_existing_lessons_strategy(
            engine, client, llm_model,
            student_name=student_name,
            teacher_name=teacher_name,
            intent_start=intent_start,
            intent_end=intent_end,
            base_check_result=check_result,
        )
        print("\n========== 方案二：挪课 ==========")
        if move_res["success"]:
            for item in move_res["candidates"]:
                print(f"  目标: {item['slot']}")
                for les in item["move_plan"]:
                    ori = les["original_lesson"]
                    print(f"    挪课: {ori.get('topic', '?')} -> {len(les['options'])} 个可选时间")

    print("\n请管理员从以上方案中选择。")


if __name__ == "__main__":
    main()
