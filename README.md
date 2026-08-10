# Harness_labs
Experimenting to make reliable coding harnesses for implementation of large complex tasks autonomously.

Everything is a work-in-progress.

The motivation behind this is twofold:
1) enable auditable cross-platform development
2) tame the overengineering tendency of frontier coding models like GPT 5.6 or Opus 5.

3) the current paradigm involves the atomic FeatureRun which develops single features in isolated worktrees by going through plan - implement - review - integrate phases. each FeatureRun consumed $3-5 API costs. 
4) as well as PlanGraph intended for decomposition of large complex tasks.
5) A PlanGraph decomposes a plan into multiple FeatureRuns that lack the plan phase (they inherit a partial plan from the PlanGraph). PlanGraph supports parallel implementation. 
