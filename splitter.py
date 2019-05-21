#!/bin/python3

# Import libraries needed for application to work

import argparse
import shutil
import gi
import gzip
import librepo
import hawkey
import tempfile
import os
import subprocess
import sys

# Look for a specific version of modulemd. The 1.x series does not
# have the tools we need.
try:
    gi.require_version('Modulemd', '2.0')
    from gi.repository import Modulemd
except:
    print("We require newer vesions of modulemd than installed..")
    sys.exit(0)
    
mmd = Modulemd

def _get_repoinfo(directory):
    """
    A function which goes into the given directory and sets up the
    needed data for the repository using librepo.
    Returns the LRR_YUM_REPO
    """
    with tempfile.TemporaryDirectory(prefix='elsplit_librepo_') as lrodir:
        h = librepo.Handle()
        h.setopt(librepo.LRO_URLS, ["file://%s" % directory])
        h.setopt(librepo.LRO_REPOTYPE, librepo.LR_YUMREPO)
        h.setopt(librepo.LRO_DESTDIR, lrodir)
        h.setopt(librepo.LRO_LOCAL, True)
        h.setopt(librepo.LRO_IGNOREMISSING, False)
        r = h.perform()
        return r.getinfo(librepo.LRR_YUM_REPO)

def _get_hawkey_sack(repo_info):
    """
    A function to pull in the repository sack from hawkey.
    Returns the sack.
    """
    hk_repo = hawkey.Repo("")
    hk_repo.filelists_fn = repo_info["filelists"]
    hk_repo.primary_fn = repo_info["primary"]
    hk_repo.repomd_fn = repo_info["repomd"]

    primary_sack = hawkey.Sack()
    primary_sack.load_repo(hk_repo, build_cache=False)
    
    return primary_sack

def _get_filelist(package_sack):
    """
    Determine the file locations of all packages in the sack. Use the
    package-name-epoch-version-release-arch as the key.
    Returns a dictionary.
    """
    pkg_list = {}
    for pkg in hawkey.Query(package_sack):
        nevr="%s-%s:%s-%s.%s"% (pkg.name,pkg.epoch,pkg.version,pkg.release,pkg.arch)
        pkg_list[nevr] = pkg.location
    return pkg_list

def _parse_repository_non_modular(package_sack, repo_info, modpkgset):
    """
    Simple routine to go through a repo, and figure out which packages
    are not in any module. Add the file locations for those packages
    so we can link to them.
    Returns a set of file locations.
    """
    sack = package_sack
    pkgs = set()

    for pkg in hawkey.Query(sack):
        if pkg.location in modpkgset:
            continue
        pkgs.add(pkg.location)
    return pkgs

def _parse_repository_modular(repo_info,package_sack):
    """
    Returns a dictionary of packages indexed by the modules they are
    contained in.
    """
    cts = {}
    idx = mmd.ModuleIndex()
    with gzip.GzipFile(filename=repo_info['modules'], mode='r') as gzf:
        mmdcts = gzf.read().decode('utf-8')
        res, failures = idx.update_from_string(mmdcts, True)
        if len(failures) != 0:
            raise Exception("YAML FAILURE: FAILURES: %s" % failures)
        if not res:
            raise Exception("YAML FAILURE: res != True")

    pkgs_list = _get_filelist(package_sack)
    idx.upgrade_streams(2)
    for modname in idx.get_module_names():
        mod = idx.get_module(modname)
        for stream in mod.get_all_streams():
            templ = list()
            for pkg in stream.get_rpm_artifacts():
                if pkg in pkgs_list:
                    templ.append(pkgs_list[pkg])
                else:
                    continue
            cts[stream.get_NSVCA()] = templ
                
    return cts


def _get_modular_pkgset(mod):
    """
    Takes a module and goes through the moduleset to determine which
    packages are inside it. 
    Returns a list of packages
    """
    pkgs = set()

    for modcts in mod.values():
        for pkg in modcts:
            pkgs.add(pkg)

    return list(pkgs)


def validate_filenames(directory, repoinfo):
    """
    Take a directory and repository information. Test each file in
    repository to exist in said module. This stops us when dealing
    with broken repositories or missing modules.
    Returns True if no problems found. False otherwise.
    """
    isok = True
    for modname in repoinfo:
        for pkg in repoinfo[modname]:
            if not os.path.exists(os.path.join(directory, pkg)):
                isok = False
                print("Path %s from mod %s did not exist" % (pkg, modname))
    return isok


def _perform_action(src, dst, action):
    """
    Performs either a copy, hardlink or symlink of the file src to the
    file destination.
    Returns None
    """
    if action == 'copy':
        try:
            shutil.copy(src, dst)
        except FileNotFoundError:
            # Missing files are acceptable: they're already checked before
            # this by validate_filenames.
            pass
    elif action == 'hardlink':
        os.link(src, dst)
    elif action == 'symlink':
        os.symlink(src, dst)


def perform_split(repos, args):
    for modname in repos:
        targetdir = os.path.join(args.target, modname)
        os.mkdir(targetdir)

        for pkg in repos[modname]:
            _, pkgfile = os.path.split(pkg)
            _perform_action(
                os.path.join(args.repository, pkg),
                os.path.join(targetdir, pkgfile),
                args.action)


def create_repos(target, repos):
    """
    Routine to create repositories. Input is target directory and a
    list of repositories.
    Returns None
    """
    for modname in repos:
        subprocess.run([
            'createrepo_c', os.path.join(target, modname),
            '--no-database'])


def parse_args():
    """
    A standard argument parser routine which pulls in values from the
    command line and returns a parsed argument dictionary.
    """
    parser = argparse.ArgumentParser(description='Split repositories up')
    parser.add_argument('repository', help='The repository to split')
    parser.add_argument('--action', help='Method to create split repos files',
                        choices=('hardlink', 'symlink', 'copy'),
                        default='hardlink')
    parser.add_argument('--target', help='Target directory for split repos')
    parser.add_argument('--skip-missing', help='Skip missing packages',
                        action='store_true', default=False)
    parser.add_argument('--create-repos', help='Create repository metadatas',
                        action='store_true', default=False)
    return parser.parse_args()


def setup_target(args):
    """
    Checks that the target directory exists and is empty. If not it
    exits the program.  Returns nothing.
    """
    if args.target:
        args.target = os.path.abspath(args.target)
        if os.path.exists(args.target):
            if not os.path.isdir(args.target):
                raise ValueError("Target must be a directory")
            elif len(os.listdir(args.target)) != 0:
                raise ValueError("Target must be empty")
        else:
            os.mkdir(args.target)

def parse_repository(directory):
    """
    Parse a specific directory, returning a dict with keys module NSVC's and
    values a list of package NVRs.
    The dict will also have a key "non_modular" for the non-modular packages.
    """
    directory = os.path.abspath(directory)
    repo_info = _get_repoinfo(directory)

    # Sometimes you get someone who blindly runs this against any
    # repository they find.  Let them know this is meant to work only
    # on repositories with modules.
    if 'modules' not in repo_info:
        print("This repository has no modules defined.")
        print("Grobisplitter only works on repos with modules.")
        sys.exit(0)

    package_sack = _get_hawkey_sack(repo_info)
    _get_filelist(package_sack)
    mod = _parse_repository_modular(repo_info,package_sack)
    modpkgset = _get_modular_pkgset(mod)
    non_modular = _parse_repository_non_modular(package_sack,repo_info, modpkgset)

    mod['non_modular'] = non_modular

    return mod

def main():
    # Determine what the arguments are and 
    args = parse_args()

    # Go through arguments and act on their values.
    setup_target(args)

    repos = parse_repository(args.repository)

    if not args.skip_missing:
        if not validate_filenames(args.repository, repos):
            raise ValueError("Package files were missing!")
    if args.target:
        perform_split(repos, args)
        if args.create_repos:
            create_repos(args.target, repos)

if __name__ == '__main__':
    main()
