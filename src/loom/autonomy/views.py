"""Page-specific reads; inactive tabs do not load their historical collections."""

VIEW_FIELDS = {
    'overview': {'goals', 'work_packages', 'learning'},
    'goals': {'goals', 'work_packages', 'stages', 'roadmaps', 'objective_planning', 'stage_reviews'},
    'work': {'goals', 'work_packages', 'learning', 'stages', 'objective_planning'},
    'profile': {'guidance'},
    'knowledge': set(),
    'learning': {'guidance', 'learning', 'work_packages'},
}
