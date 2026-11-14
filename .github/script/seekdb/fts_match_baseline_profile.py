#!/usr/bin/env python3
"""Profile the preserved match-filter baseline without rebuilding it."""
import json

import fts_accepted_profile as profile
import fts_ik_result_array_validation as experiment


validation, driver = experiment.validation, experiment.driver
ORIGINAL_SAFE_ELF = driver.safe_elf


def source_proof(args):
    proof = experiment.ORIGINAL_SOURCE_PROOF(args, experiment.PRODUCTS)
    return dict(proof, experiment='PRESERVED_MATCH_BASELINE_PROFILE',
                profiledSourceCommit=experiment.BASE,
                profiledBinaryCommit=experiment.ARCHIVED_BASE,
                profiledSourceTree=experiment.TREE)


def safe_elf(path, digest, size, build_id, commit):
    driver.require(commit in (experiment.BASE, experiment.ARCHIVED_BASE),
                   'Unexpected executable identity for baseline-only profiling')
    return ORIGINAL_SAFE_ELF(path, digest, size, build_id, experiment.ARCHIVED_BASE)


def prepare(args):
    profile.prepare(args)
    plan_path = args.root / 'plan.json'
    plan = json.loads(plan_path.read_text())
    plan.update(scope='PRESERVED_MATCH_BASELINE_PROFILE_NOT_PERFORMANCE_COMPARISON',
                sourceCommit=experiment.BASE, binaryCommit=experiment.ARCHIVED_BASE,
                sourceTree=experiment.TREE)
    driver.dump(plan_path, plan)
    report_path = args.root / 'report.json'
    report = json.loads(report_path.read_text())
    report['plan'] = plan
    driver.dump(report_path, report)


def validate(args):
    profile.validate(args)
    report_path = args.root / 'report.json'
    report = json.loads(report_path.read_text())
    report.update(profiledSourceCommit=experiment.BASE,
                  profiledBinaryCommit=experiment.ARCHIVED_BASE,
                  profiledSourceTree=experiment.TREE)
    driver.dump(report_path, report)
    manifest_path = args.root / 'profiling/accepted-benchmark/profile-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest.update(sourceCommit=experiment.BASE, binaryCommit=experiment.ARCHIVED_BASE,
                    sourceTree=experiment.TREE)
    driver.dump(manifest_path, manifest)


def configure():
    for key, value in experiment.PIN.items():
        setattr(validation, key, value)
        setattr(driver, key, value)
    validation.source_proof = driver.source_proof = source_proof
    driver.safe_elf = safe_elf
    driver.prepare = prepare
    driver.validate = validate


if __name__ == '__main__':
    configure()
    raise SystemExit(validation.main())
