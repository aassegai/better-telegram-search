"""Current pending unique media work, assigned to the same first eligible chat as workers."""


def pending_condition(stage, *, future=False):
    if stage == "ocr":
        return (
            "NOT EXISTS (SELECT 1 FROM ocr_cache o WHERE o.sha256=r.sha256 "
            "AND o.version=:version AND o.state IN ('ready','failed'))"
        )
    if stage == "clip":
        return (
            "NOT EXISTS (SELECT 1 FROM media_embeddings e WHERE e.sha256=r.sha256 "
            "AND e.space_id=:space AND e.kind='image') AND NOT EXISTS "
            "(SELECT 1 FROM media_failures f WHERE f.sha256=r.sha256 AND f.space_id=:space)"
        )
    dense = (
        "EXISTS (SELECT 1 FROM ocr_cache o WHERE o.sha256=r.sha256 "
        "AND o.version=:version AND o.state='ready' AND o.text<>'') AND NOT EXISTS "
        "(SELECT 1 FROM media_embeddings e WHERE e.sha256=r.sha256 AND e.kind='ocr' "
        "AND e.space_id=:space AND e.ocr_version=:version) AND NOT EXISTS "
        "(SELECT 1 FROM media_failures f WHERE f.sha256=r.sha256 AND f.space_id=:failure)"
    )
    return "(" + pending_condition("ocr") + " OR (" + dense + "))" if future else dense


def pending_groups(conn, stage, space, version, failure):
    column = {"ocr": "ocr_paused", "clip": "media_paused", "ocr_dense": "ocr_dense_paused"}[stage]
    return conn.execute(
        "SELECT c.id,c.image_batch,c.text_batch,c.ocr_batch,c.ocr_region_batch,COUNT(*) photos,"
        "COALESCE(SUM((SELECT length(o.text) FROM ocr_cache o WHERE o.sha256=p.sha256 "
        "AND o.version=:version)),0) characters FROM (SELECT r.sha256,MIN(r.chat_id) owner "
        "FROM indexable_media_refs r JOIN chats h ON h.id=r.chat_id "
        f"WHERE h.{column}=0 AND r.kind='photo' AND r.status='ready' AND "
        + pending_condition(stage)
        + " GROUP BY r.sha256) p JOIN chats c ON c.id=p.owner GROUP BY c.id",
        {"space": space, "version": version, "failure": failure},
    ).fetchall()
