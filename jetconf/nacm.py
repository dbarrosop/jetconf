import json
import collections
import logging
from threading import Lock

import colorlog
import sys
from enum import Enum, unique
from colorlog import error, warning as warn, info, debug
from typing import List, Any, Dict, TypeVar, Tuple, Set
from jetconf.yang_json_path import YangJsonPath
import copy

JsonNodeT = Dict[str, Any]


class Action(Enum):
    PERMIT = True
    DENY = False


class Permission(Enum):
    NACM_ACCESS_READ = 0
    NACM_ACCESS_CREATE = 1
    NACM_ACCESS_UPDATE = 2
    NACM_ACCESS_DELETE = 3
    NACM_ACCESS_EXEC = 4


class NacmRuleType(Enum):
    NACM_RULE_NOTSET = 0
    NACM_RULE_OPERATION = 1
    NACM_RULE_NOTIF = 2
    NACM_RULE_DATA = 3


class JsonDoc:
    def __init__(self, root: JsonNodeT, root_prefix_path: YangJsonPath = None):
        self.root = root
        self.root_prefix_path = root_prefix_path

    # Selects data node specified by path in current document
    # Returns: chain of data nodes from root to selected node, or None
    def select_data_node(self, path: YangJsonPath) -> List[JsonNodeT]:
        data_node = self.root
        _last_ns = None
        _segs_to_search = path.path_segments
        ret = [data_node]

        # Only absolute path can select nodes
        if not path.is_absolute():
            return None

        # Beginning of 'path' argument must match the prefix
        if self.root_prefix_path is not None:
            if len(self.root_prefix_path.path_segments) > len(path.path_segments):
                return None
            else:
                for i in range(0, len(self.root_prefix_path.path_segments)):
                    if path.path_segments[i] != self.root_prefix_path.path_segments[i]:
                        return None
                    _last_ns = path.path_segments[i].ns

                _rel_path_idx = len(self.root_prefix_path.path_segments)
                _segs_to_search = path.path_segments[_rel_path_idx:]

        for segment in _segs_to_search:
            # Ignore empty segment, i.e. "//"
            if segment.val == "":
                continue

            seg_str = None
            if segment.ns != _last_ns:
                seg_str = segment.get_val(fully_qualified=True)
                _last_ns = segment.ns
            else:
                seg_str = segment.get_val()
            data_node = data_node.get(seg_str)
            ret.append(data_node)

            if isinstance(data_node, dict):
                if segment.select is not None:
                    error("Redundant selector \"{}\", node is not a list".format(segment.select))
                    return None
            elif isinstance(data_node, list):
                # Node is a list but has no selector specified
                if segment.select is None:
                    # Only last path segment can select a whole list
                    if segment is not path.path_segments[-1]:
                        error("Node \"{}\" is a list, but no selector is present in path".format(segment))
                        return None
                    else:
                        # return data_node
                        return ret
                # Node is a list and has a selector
                else:
                    selected_nodes = list(
                        filter(
                            lambda x: (x.get(segment.select[0]) == segment.select[1]) if isinstance(x, dict) else False,
                            data_node
                        )
                    )
                    if len(selected_nodes) > 1:
                        # Multiple nodes matched by selector
                        error("Ambiguous selector \"{}\" of path segment \"{}\" in path \"{}\"".format(segment.select,
                                                                                                       segment.val,
                                                                                                       path))
                        return None
                    elif selected_nodes is None or len(selected_nodes) == 0:
                        # No nodes selected by selector
                        return None
                    else:
                        data_node = selected_nodes[0]
                        ret[-1] = selected_nodes[0]
            elif data_node is None:
                # Node does not exist in data
                return None
            else:
                error("Invalid type of data node: \"{}\"".format(type(data_node)))
                return None

                # print("seg={}".format(segment))
                # print(data_node)
        # return data_node
        return ret


class NacmGroup:
    def __init__(self, name: str, users: List[str]):
        self.name = name
        self.users = users


class NacmRule:
    class TypeData:
        def __init__(self):
            self.path = None  # type: YangJsonPath
            self.rpc_names = None
            self.ntf_names = None

    def __init__(self):
        self.name = None
        self.comment = None
        self.module = None
        # NacmRuleType
        self.type = NacmRuleType.NACM_RULE_NOTSET
        # path, rpc_names, ntf_names
        self.type_data = self.TypeData()
        # Permission
        self.access = set()
        self.action = Action.DENY


class NacmRuleList:
    def __init__(self):
        self.name = ""
        self.groups = []
        self.rules = []


class NacmConfig:
    def __init__(self):
        self.json = None  # type: JsonDoc
        self._nacm_json = None
        self.enabled = False
        self.default_read = Action.PERMIT
        self.default_write = Action.PERMIT
        self.default_exec = Action.PERMIT
        self.nacm_groups = []
        self.rule_lists = []
        self._data_lock = Lock()
        self._lock_username = None

    def load_json(self, filename: str):
        with open(filename, "rt") as fp:
            self.json = JsonDoc(json.load(fp))
            self._nacm_json = self.json.root.get("ietf-netconf-acm:nacm")

        if self._nacm_json is None:
            error("Cannot load json file " + filename + "or it does not contain \"ietf-netconf-acm:nacm\" root element")
            return

        self.enabled = self._nacm_json["enable-nacm"]
        self.default_read = Action.PERMIT if self._nacm_json["read-default"] == "permit" else Action.DENY
        self.default_write = Action.PERMIT if self._nacm_json["write-default"] == "permit" else Action.DENY
        self.default_exec = Action.PERMIT if self._nacm_json["exec-default"] == "permit" else Action.DENY

        for group in self._nacm_json["groups"]["group"]:
            self.nacm_groups.append(NacmGroup(group["name"], group["user-name"]))

        for rule_list_json in self._nacm_json["rule-list"]:
            rl = NacmRuleList()
            rl.name = rule_list_json["name"]
            rl.groups = rule_list_json["group"]

            for rule_json in rule_list_json["rule"]:
                rule = NacmRule()
                rule.name = rule_json.get("name")
                rule.comment = rule_json.get("comment")
                rule.module = rule_json.get("module-name")

                if rule_json.get("access-operations") is not None:
                    access_perm_list = rule_json["access-operations"].split()
                    for access_perm_str in access_perm_list:
                        access_perm = {
                            "read": Permission.NACM_ACCESS_READ,
                            "create": Permission.NACM_ACCESS_CREATE,
                            "update": Permission.NACM_ACCESS_UPDATE,
                            "delete": Permission.NACM_ACCESS_DELETE,
                            "exec": Permission.NACM_ACCESS_EXEC,
                            "*": set(Permission),
                        }.get(access_perm_str)
                        if access_perm is not None:
                            if isinstance(access_perm, collections.Iterable):
                                rule.access.update(access_perm)
                            else:
                                rule.access.add(access_perm)

                if rule_json.get("rpc-name") is not None:
                    if rule.type != NacmRuleType.NACM_RULE_NOTSET:
                        error(
                                "Invalid rule definition (multiple cases from rule-type choice): \"{}\"".format(
                                        rule.name))
                    else:
                        rule.type = NacmRuleType.NACM_RULE_OPERATION
                        rule.type_data.rpc_names = rule_json.get("rpc-name").split()

                if rule_json.get("notification-name") is not None:
                    if rule.type != NacmRuleType.NACM_RULE_NOTSET:
                        error(
                                "Invalid rule definition (multiple cases from rule-type choice): \"{}\"".format(
                                        rule.name))
                    else:
                        rule.type = NacmRuleType.NACM_RULE_NOTIF
                        rule.type_data.ntf_names = rule_json.get("notification-name").split()

                if rule_json.get("path") is not None:
                    if rule.type != NacmRuleType.NACM_RULE_NOTSET:
                        error(
                                "Invalid rule definition (multiple cases from rule-type choice): \"{}\"".format(
                                        rule.name))
                    else:
                        rule.type = NacmRuleType.NACM_RULE_DATA
                        # i.e. /ietf-interfaces:interfaces/interface[name='eth0']/ietf-ip:ipv4/ip
                        rule.type_data.path = YangJsonPath(rule_json["path"])

                rule.action = Action.PERMIT if rule_json["action"] == "permit" else Action.DENY
                rl.rules.append(rule)

            self.rule_lists.append(rl)

    def lock_data(self, username: str = None):
        res = self._data_lock.acquire(blocking=False)
        if res:
            self._lock_username = username or "(unknown)"
            debug("Acquired data lock for user {}".format(username))
            info("Acquired data lock for user {}".format(username))
        else:
            debug("Failed to acquire lock for user {}, already locked by {}".format(username, self._lock_username))
            info("Failed to acquire lock for user {}, already locked by {}".format(username, self._lock_username))
        return res

    def unlock_data(self):
        self._data_lock.release()
        debug("Released data lock for user {}".format(self._lock_username))
        info("Released data lock for user {}".format(self._lock_username))
        self._lock_username = None


# Rules for particular session (logged-in user)
class NacmRpc:
    # "username" only for testing, will be part of "session"
    def __init__(self, config: NacmConfig, session: Any, username: str):
        self.default_read = config.default_read
        self.default_write = config.default_write
        self.default_exec = config.default_exec
        user_groups = list(filter(lambda x: username in x.users, config.nacm_groups))
        user_groups_names = list(map(lambda x: x.name, user_groups))
        self.rule_lists = list(filter(lambda x: (set(user_groups_names) & set(x.groups)), config.rule_lists))

    def check_data_node(self, node: JsonNodeT, doc: JsonDoc, access: Permission) -> bool:
        for rl in self.rule_lists:
            for rule in rl.rules:
                debug("Checking rule \"{}\"".format(rule.name))

                # 1. Module name
                # TODO Validate against data model
                debug("- Checking module name")
                data_model_module_name = rule.module
                if not (rule.module == "*" or rule.module == data_model_module_name):
                    # rule does not match
                    continue

                # 3. access - do it before 2 for optimize, the 2nd step is the most difficult
                debug("- Checking access specifier")
                if access not in rule.access:
                    # rule does not match
                    continue

                # 2. type and operation name
                debug("- Checking type and operation name")
                if rule.type == NacmRuleType.NACM_RULE_NOTSET:
                    info("Rule found: {}".format(rule.name))
                    return rule.action

                if rule.type != NacmRuleType.NACM_RULE_DATA or not rule.type_data.path:
                    continue
                _selected = doc.select_data_node(rule.type_data.path)
                if (_selected is not None) and (_selected[-1] is node):
                    # Success!
                    # the path selects the node
                    info("Rule found: \"{}\"".format(rule.name))
                    return rule.action

        # no rule found
        # default action
        info("No rule found, returning default action")
        if access == Permission.NACM_ACCESS_READ:
            return self.default_read
        elif access in (Permission.NACM_ACCESS_CREATE, Permission.NACM_ACCESS_DELETE, Permission.NACM_ACCESS_UPDATE):
            return self.default_write
        else:
            # unknown access request - deny
            return Action.DENY

    def _check_data_read_recursion(self, node: JsonNodeT, doc: JsonDoc):
        if isinstance(node, dict):
            for child_key in node.keys():
                # Do not check leaves
                if not (isinstance(node[child_key], dict) or isinstance(node[child_key], list)):
                    continue

                if self.check_data_node(node[child_key], doc, Permission.NACM_ACCESS_READ) == Action.DENY:
                    debug("Pruning node {} {}".format(id(node[child_key]), node[child_key]))
                    node[child_key] = None
                else:
                    self._check_data_read_recursion(node[child_key], doc)
        elif isinstance(node, list):
            for i in range(0, len(node)):
                # Do not check leaves
                if not (isinstance(node[i], dict) or isinstance(node[i], list)):
                    continue

                if self.check_data_node(node[i], doc, Permission.NACM_ACCESS_READ) == Action.DENY:
                    debug("Pruning node {} {}".format(id(node[i]), node[i]))
                    node[i] = None
                else:
                    self._check_data_read_recursion(node[i], doc)

    def check_data_read(self, node: JsonNodeT, doc: JsonDoc):
        self._check_data_read_recursion(node, doc)


if __name__ == "__main__":
    colorlog.basicConfig(format="%(asctime)s %(log_color)s%(levelname)-8s%(reset)s %(message)s", level=logging.INFO,
                         stream=sys.stdout)
    nacm = NacmConfig()
    nacm.load_json("example-data.json")
    rpc = NacmRpc(nacm, None, "dominik")

    test_paths = (
        (
            "/dns-server:dns-server-state/zone[domain='example.com']/statistics/opcodes/opcode-count[opcode='query']",
            Permission.NACM_ACCESS_UPDATE,
            Action.DENY
        ),
        (
            "/dns-server:dns-server/zones/zone",
            Permission.NACM_ACCESS_READ,
            Action.PERMIT
        ),
        (
            "/ietf-netconf-acm:nacm/groups",
            Permission.NACM_ACCESS_READ,
            Action.PERMIT
        ),
        (
            "/ietf-netconf-acm:nacm/groups/group[name='admin']",
            Permission.NACM_ACCESS_READ,
            Action.DENY
        )
    )

    for test_path in test_paths:
        info("Testing path \"{}\"".format(test_path[0]))
        test_path_obj = YangJsonPath(test_path[0])
        datanodes = nacm.json.select_data_node(test_path_obj)
        if datanodes:
            info("Node found")
            debug("Node contents: {}".format(datanodes[-1]))
            action = rpc.check_data_node(datanodes[-1], nacm.json, test_path[1])
            if action == test_path[2]:
                info("Action = {}, {}\n".format(action.name, "OK"))
            else:
                warn("Action = {}, {}\n".format(action.name, "FAILED"))
        else:
            info("Node not found!")

    parsed_url = YangJsonPath("/ietf-netconf-acm:nacm/groups")
    _node = copy.deepcopy(nacm.json.select_data_node(parsed_url)[-1])
    if not _node:
        print("Node null")
    _doc = JsonDoc(_node, parsed_url)
    _rpc = NacmRpc(nacm, None, "dominik")
    _rpc.check_data_read(_node, _doc)
    print("result = {}".format(_doc.root))
    if _doc.root == {'group': [None, {'user-name': ['lada', 'pavel', 'dominik', 'lojza@mail.cz'], 'name': 'users'}]}:
        info("OK")
    else:
        warn("FAILED")