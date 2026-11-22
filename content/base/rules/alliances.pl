:- encoding(utf8).
% =====================================================================
% base 包：盟友与联盟。
%   (:Person)-[:ALLY]->(:Person)                         盟友（双向各一条）
%   (:Person)-[:MEMBER_OF]->(:Alliance {name})           结盟的人自然聚成一伙
%   (:Alliance)-[:STANCE {value}]->(:Alliance)           联盟之间的关系 -10..10
% 每两个游戏小时分四步（每步之前引擎重读世界）：
%   bonds 互相好感都够高就结盟、好感跌破 0 的盟约破裂 → leave 不再喜欢同伴的人退出
%   → dissolve 少于两人的联盟解散 → stance 联盟之间的关系 = 两边成员互相好感的平均
% =====================================================================

const(auto_ally, 6).          % 互相好感都到这么高就自然成了盟友
const(ally_min, 4).           % 提出结盟时，对方对你的好感至少这么多
const(stance_friendly, 4).    % 联盟之间：友好（以上）
const(stance_rival, -2).      % 联盟之间：对立（以下）；敌对见 social.pl 的 alliance_hostile

would_ally(Other, Me) :- feeling(Other, Me, V), const(ally_min, M), V >= M.

alliance_of(P, G) :- once(in_alliance(P, G)).

alliances_at_war(A, B) :-
    alliance_of(A, GA), alliance_of(B, GB), GA \== GB,
    stance(GA, GB, S), const(alliance_hostile, H), S =< H.

stance_word(V, friendly) :- const(stance_friendly, F), V >= F, !.
stance_word(V, hostile) :- const(alliance_hostile, H), V =< H, !.
stance_word(V, rivals) :- const(stance_rival, R), V =< R, !.
stance_word(_, neutral).

% a、b 刚结盟：都没有联盟就新成立一个；一方有就把另一方拉进来；两边各有联盟就让两个联盟更亲近
join_effect(A, B, _, E) :-
    alliance_of(A, GA), alliance_of(B, GB), !,
    GA \== GB,
    ( stance(GA, GB, S0) -> true ; S0 = 0 ), const(stance_friendly, F), S is max(S0, F),
    member(X-Y, [GA-GB, GB-GA]),
    E = _{op: set_rel_prop, a: X, a_label: 'Alliance', b: Y, b_label: 'Alliance', rel: 'STANCE', prop: value, value: S}.
join_effect(A, B, _, E) :- alliance_of(A, G), !, member_of(B, G, E).
join_effect(A, B, _, E) :- alliance_of(B, G), !, member_of(A, G, E).
join_effect(A, B, T, E) :-
    format(atom(G), "~w's Band", [A]),
    (   E = _{op: merge_node, label: 'Alliance', name: G, props: _{founded: T, founder: A}}
    ;   member_of(A, G, E)
    ;   member_of(B, G, E)
    ).

member_of(P, G, _{op: link, a: P, a_label: 'Person', b: G, b_label: 'Alliance', rel: 'MEMBER_OF'}).

ally_link(A, B, _{op: link, a: A, a_label: 'Person', b: B, b_label: 'Person', rel: 'ALLY'}).

% ---------------------------------------------------------------------
% 时间
% ---------------------------------------------------------------------

tick_phase(10, bonds).
tick_phase(20, leave).
tick_phase(30, dissolve).
tick_phase(40, stance).

due :- const(decay_every, P), every(P).

tick(bonds, E) :-
    due, new_allies(A, B), now(T),
    ( ally_link(A, B, E) ; ally_link(B, A, E) ; join_effect(A, B, T, E) ).
tick(bonds, _{op: unlink, a: A, a_label: 'Person', b: B, b_label: 'Person', rel: 'ALLY'}) :-
    due, ally(A, B), rel_prop('FEELS', A, B, value, V), V < 0.

new_allies(A, B) :-
    const(auto_ally, M),
    rel_prop('FEELS', A, B, value, V1), V1 >= M, A @< B,
    rel_prop('FEELS', B, A, value, V2), V2 >= M,
    \+ ally(A, B).

tick(leave, _{op: unlink, a: P, a_label: 'Person', b: G, b_label: 'Alliance', rel: 'MEMBER_OF'}) :-
    due, node('Alliance', G), in_alliance(P, G),
    findall(V, (in_alliance(O, G), O \== P, feeling(P, O, V)), Vs), Vs \== [],
    sum_list(Vs, S), S < 0.

tick(dissolve, _{op: delete_entity, entity: G, label: 'Alliance'}) :-
    due, node('Alliance', G), aggregate_all(count, in_alliance(_, G), N), N < 2.

tick(stance, E) :-
    due, node('Alliance', GA), node('Alliance', GB), GA @< GB,
    findall(V, (in_alliance(X, GA), in_alliance(Y, GB), ( feeling(X, Y, V) ; feeling(Y, X, V) )), Vs),
    Vs \== [], sum_list(Vs, S), length(Vs, N),
    M is max(-10, min(10, round(S / N))),
    member(P-Q, [GA-GB, GB-GA]),
    E = _{op: set_rel_prop, a: P, a_label: 'Alliance', b: Q, b_label: 'Alliance', rel: 'STANCE', prop: value, value: M}.
