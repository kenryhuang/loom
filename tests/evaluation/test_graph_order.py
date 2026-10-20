from loom.evaluation.goal_loops.graph_order import hierarchy_order, order_goals


def test_action_order_is_independent_of_declaration_order_and_does_not_reparent():
    goals = [
        dict(id="later", parent_goal_id="root", origin_line=1),
        dict(id="child", parent_goal_id="earlier", origin_line=40, root_relation="Resolve a component"),
        dict(id="root", parent_goal_id=None, origin_line=0),
        dict(id="earlier", parent_goal_id="root", origin_line=30),
    ]
    cards = [dict(id="a", start_line=10, end_line=15), dict(id="b", start_line=20, end_line=25)]
    attrs = [dict(card_id="a", goal_id="child"), dict(card_id="b", goal_id="later")]
    order_goals(goals, cards, attrs)
    assert [g["id"] for g in goals] == ["root", "earlier", "child", "later"]
    assert [g["id"] for g in hierarchy_order(goals)] == ["root", "earlier", "child", "later"]
    assert [(g["first_action_line"], g["last_action_line"]) for g in goals] == [(10, 25), (10, 15), (10, 15), (20, 25)]
    assert goals[2]["parent_goal_id"] == "earlier"
    assert goals[2]["parent_relation_basis"] == "inferred"
    assert goals[1]["parent_relation_basis"] == "unreviewed"
    order_goals(goals, cards, list(reversed(attrs)))
    assert [g["id"] for g in goals] == ["root", "earlier", "child", "later"]
