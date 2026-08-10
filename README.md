# Harness_labs
Experimenting to make reliable coding harnesses for implementation of large complex tasks autonomously.

Everything is a work-in-progress. Main is deliberately empty; see branches. 

The motivation behind this is twofold:
1) enable auditable cross-platform development
2) tame the overengineering tendency of frontier coding models like GPT 5.6 or Opus 5.

The current paradigm involves:
1) FeatureRun - develops single features in isolated worktrees by going through plan - implement - review - integrate phases. Each FeatureRun consumes $3-5 API costs in testing.
2) PlanGraph intended for decomposition of large complex tasks. PlanGraph decomposes a plan into multiple FeatureRuns that lack the plan phase (they inherit a partial plan from the PlanGraph). PlanGraph supports parallel implementation. 
