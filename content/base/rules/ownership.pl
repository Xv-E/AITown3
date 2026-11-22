:- encoding(utf8).
% =====================================================================
% base 包：东西归谁、送礼和买卖带来的好感。
%   (:Person)-[:OWNS]->(:Item)   这件东西是他的（拿在谁手上、放在哪都不变）
% 偷窃见 social.pl。词汇（owner / player / feeling ...）也在 social.pl。
% =====================================================================

reads(rel('HAS_ITEM')).

const(gift, 3).                 % 收到礼物：好感
const(player_gift_trust, 2).    % 玩家送礼：信任
const(returned, 3).             % 东西还给了失主：好感
const(trade, 1).                % 做成一笔买卖：双方好感

% 不管做了什么动作：手上没主的东西归做的人（捡到、采集、做出来、一开始就拿着）
after(Event, _{op: link, a: A, a_label: 'Person', b: I, b_label: 'Item', rel: 'OWNS'}) :-
    compound(Event), arg(1, Event, A), rel('HAS_ITEM', A, I), \+ owner(I, _).

% 送东西：是自己的才转给对方；收到的人好感上升；还给失主再多一些
after(give(G, I, R, _), E) :- now(T), given(G, I, R, T, E).

given(G, I, _, _, _{op: unlink, a: G, a_label: 'Person', b: I, b_label: 'Item', rel: 'OWNS'}) :- owner(I, G).
given(G, I, R, _, _{op: link, a: R, a_label: 'Person', b: I, b_label: 'Item', rel: 'OWNS'}) :- owner(I, G).
given(G, I, R, T, _{op: feel, a: R, b: G, delta: D, reason: Why, at: T}) :-
    gift_delta(G, D), format(atom(Why), "gave me ~w", [I]).
given(G, I, R, T, _{op: feel, a: R, b: G, delta: D, reason: Why, at: T}) :-
    owner(I, R), const(returned, D), format(atom(Why), "gave my ~w back", [I]).
given(G, I, R, _, _{op: note, text: Text}) :-
    player(G), feeling(R, G, V0), gift_delta(G, D1),
    ( owner(I, R) -> const(returned, D2) ; D2 = 0 ),
    feeling_range(player_trust, Lo, Hi), V is max(Lo, min(Hi, V0 + D1 + D2)),
    format(atom(Text), "~w trust → ~w", [R, V]).

gift_delta(G, D) :- ( player(G) -> const(player_gift_trust, D) ; const(gift, D) ).

% 买卖：东西归买的人；双方好感各升一点
after(buy(C, I, S, _), E) :- now(T), traded(C, S, I, C, T, E).
after(sell(C, I, B, _), E) :- now(T), traded(C, B, I, B, T, E).

traded(_, _, I, Buyer, _, _{op: unlink, a: O, a_label: 'Person', b: I, b_label: 'Item', rel: 'OWNS'}) :-
    owner(I, O), O \== Buyer.
traded(_, _, I, Buyer, _, _{op: link, a: Buyer, a_label: 'Person', b: I, b_label: 'Item', rel: 'OWNS'}).
traded(X, Y, _, _, T, _{op: feel, a: Y, b: X, delta: D, reason: 'a fair deal', at: T}) :- const(trade, D).
traded(X, Y, _, _, T, _{op: feel, a: X, b: Y, delta: D, reason: 'a fair deal', at: T}) :- const(trade, D).
