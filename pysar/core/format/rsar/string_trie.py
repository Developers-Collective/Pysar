# Please do not ask how this works
# just accept it
import io
import struct
import uuid
from typing import BinaryIO


class _StringTrieNode:
    def __init__(self, left, right, char_index, bit, data):
        self.uuid = uuid.uuid4()
        self.left = left
        self.right = right
        self.char_index = char_index
        self.bit = bit
        self.data = data
        self.is_leaf = data is not None

    def value(self):
        return self.data.s if self.data else None

    def string_idx(self):
        return self.data.str_idx if self.data else None

    def info_index(self):
        return self.data.info_idx if self.data else None

    def has_less_restrictive_path(self, other):
        if other.is_leaf:
            return True
        if not self.is_leaf and self.char_index < other.char_index:
            return True
        return self.char_index == other.char_index and self.bit < other.bit

    def __bytes__(self) -> bytes:
        info_idx = self.info_index()
        info_idx = -1 if info_idx is None else info_idx

        string_idx = self.string_idx()
        string_idx = -1 if string_idx is None else string_idx

        return struct.pack('>hhiiii', self.is_leaf, self.bit, self.left, self.right, string_idx,
                           info_idx)

    def __hash__(self):
        return hash(self.uuid)

    def __eq__(self, other):
        return isinstance(other, _StringTrieNode) and self.uuid == other.uuid

    def __str__(self):
        return f"Node(left={self.left}, right={self.right}, char_index={self.char_index}, bit={self.bit}, data={self.data})"


class _Entry:
    def __init__(self, s, str_idx, info_idx):
        self.s = s
        self.str_idx = str_idx
        self.info_idx = info_idx

    def __str__(self):
        return f"Entry(string={self.s}, strIdx={self.str_idx}, infoIdx={self.info_idx})"

    def __repr__(self):
        return str(self)


class _NodeReader:
    def __init__(self, flags=-1, bit=-1, left=-1, right=-1, string_index=-1, info_index=-1,
                 buffer: BinaryIO = None):
        if buffer is not None:
            self.flags, self.bit, self.left, self.right, self.string_index, self.info_index = \
                struct.unpack('>hhiiii', buffer.read(20))
        else:
            self.flags = flags
            self.bit = bit
            self.left = left
            self.right = right
            self.string_index = string_index
            self.info_index = info_index

    def str(self):
        return self.string_index

    def info(self):
        return self.info_index


class StringTrie:
    def __init__(self, buffer: BinaryIO = None, labels: list[str] = None) -> None:
        self._nodes: list[_StringTrieNode] = []
        self._root: int = -1

        super().__init__()
        if not buffer and not labels:
            return

        buffer.read(4)  # Skip the root idx, because we don't care about it
        __size = struct.unpack('>I', buffer.read(4))[0]  # Trie size

        for _ in range(__size):
            __tmp = _NodeReader(buffer=buffer)
            if __tmp.str() != -1 and __tmp.info() != -1:
                self.insert(labels[__tmp.str()], __tmp.str(), __tmp.info())

    def get_entry(self, name: str) -> tuple[str, int, int] | None:
        return next(((__entry.data.s, __entry.data.str_idx, __entry.data.info_idx) for __entry in self._nodes if
                    __entry.data and __entry.data.s == name), (None, None, None))

    def get_leafs(self):
        return [node for node in self._nodes if node.is_leaf]

    def __bytes__(self) -> bytes:
        buffer = io.BytesIO()
        buffer.write(struct.pack('>ii', self._root, len(self._nodes)))

        for node in self._nodes:
            buffer.write(bytes(node))

        return buffer.getvalue()

    def __iter__(self):
        return iter(self.get_leafs())

    def __str__(self):
        content = ", ".join(str(node) for node in self._nodes)
        return f"[{content}]"

    def num_of_leafs(self):
        return len(self.get_leafs())

    def insert(self, __str: str, str_idx: int, info_idx: int) -> None:
        self._root = self.__insert_helper(_Entry(__str, str_idx, info_idx), self._root)

    def __insert_helper(self, entry: _Entry, idx: int) -> int:
        if idx == -1:
            return self.__create_and_insert_leaf(entry)

        cur_node: _StringTrieNode = self._nodes[idx]
        if not cur_node.is_leaf:
            if self.__get_test_bit(entry, cur_node.char_index, cur_node.bit):
                old_right: int = cur_node.right
                new_right: int = self.__insert_helper(entry, cur_node.right)

                if self._nodes[new_right].has_less_restrictive_path(cur_node):
                    if self._nodes[new_right].right == old_right:
                        self._nodes[new_right].right = idx
                    else:
                        self._nodes[new_right].left = idx

                    return new_right
                else:
                    cur_node.right = new_right
            else:
                old_left: int = cur_node.left
                new_left: int = self.__insert_helper(entry, cur_node.left)

                if self._nodes[new_left].has_less_restrictive_path(cur_node):
                    if self._nodes[new_left].right == old_left:
                        self._nodes[new_left].right = idx
                    else:
                        self._nodes[new_left].left = idx

                    return new_left
                else:
                    cur_node.left = new_left

            return idx
        else:
            return self.__create_and_insert_internal_node(entry, idx)

    def __create_and_insert_internal_node(self, entry: _Entry, old_node_index: int) -> int:
        new_node_idx: int = self.__create_and_insert_leaf(entry)

        old_node: _StringTrieNode = self._nodes[old_node_index]
        new_node: _StringTrieNode = self._nodes[new_node_idx]

        smaller_node: _StringTrieNode = new_node if new_node.data.s < old_node.data.s else old_node
        bigger_node: _StringTrieNode = smaller_node == old_node and new_node or old_node
        test_info: tuple[int, int] = self.__find_test_info(smaller_node.data.s, bigger_node.data.s)
        if self.__get_test_bit(entry, test_info[0], test_info[1]):
            new_internal_node = _StringTrieNode(old_node_index, new_node_idx, test_info[0], test_info[1], None)
        else:
            new_internal_node = _StringTrieNode(new_node_idx, old_node_index, test_info[0], test_info[1], None)
        self._nodes.append(new_internal_node)
        return len(self._nodes) - 1

    @staticmethod
    def __find_test_info(smaller: str, larger: str) -> tuple[int, int]:
        padded_smaller = smaller.ljust(len(larger), '\0')
        i = 0
        while i < len(larger) and padded_smaller[i] == larger[i]:
            i += 1
        if i >= len(larger):
            raise ValueError(f"Unable to find difference between strings {smaller} {larger}")
        char_one = bin(ord(padded_smaller[i]))[2:].zfill(8)
        char_two = bin(ord(larger[i]))[2:].zfill(8)
        bit = 0
        while bit < len(char_one) and char_one[bit] == char_two[bit]:
            bit += 1
        return i, bit

    def __create_and_insert_leaf(self, entry: _Entry) -> int:
        self._nodes.append(_StringTrieNode(-1, -1, -1, -1, entry))
        return len(self._nodes) - 1

    @staticmethod
    def __get_test_bit(entry: _Entry, chr_idx: int, bit: int) -> bool:
        if chr_idx >= len(entry.s):
            return False

        bits = bin(ord(entry.s[chr_idx]))[2:].zfill(8)
        return bits[bit] == '1'
