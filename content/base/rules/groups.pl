:- encoding(utf8).
% =====================================================================
% base 包：群体。势力、一家人、城镇、帮派、行会、朋友结的伙、狼群……都是群体。
%
% 图：
%   (:Faction {name, kind, scale, traits, status, goal, description, join_policy, join_standing, trial})
%   (:Person)-[:MEMBER_OF {role, loyalty, leader}]->(:Faction)       成员（忠诚 0..10，默认 6）
%   (:Faction)-[:PART_OF]->(:Faction)                                上级群体（一家人 ⊂ 镇）
%   (:Faction)-[:FACTION_REL {stance, tension, reason}]-(:Faction)   群体之间的立场（无向，只存一条）
%   (:Person)-[:STANDING {value}]->(:Faction)                        在某群体眼里的声望 -10..10
%   (:Faction)-[:CONTROLS {strength}]->(:Location)                   地盘（见 group_life.pl）
%
% 种类（kind）只是一组默认特性：kind_traits(family, [kin, closed, ...])。
% 规则只问特性 —— has/2、mult/3、sum/3、entry/3 —— 不认种类名；
% 加一种群体、给某个群体加一种脾气，都只写数据（这里或别的包里的 kind_traits / trait）。
%
% 给引擎的出口：
%   context(Me, Text) / context(Me, Other, Text)     提示词里的处境和关系
%   after(give / attack / deed(...), E)              对人做的事传到他所在群体的声望上
%   after(ask_to_join(P, G, Via), E) / after(leave_group(P, G), E)   入会、退出（界面发起）
%   group_card(Viewer, G, Card)                      界面上的一张群体卡片
% =====================================================================

:- multifile kind_traits/2, trait/3, trait_text/2.
:- discontiguous kind_traits/2, trait/3, trait_text/2, after/2, context/2, context/3.

reads(label('Faction')).
reads(label('Location', [controlled_by, tags, parent, description])).
reads(label('Fact', [faction, text])).
reads(label('Quest', [kind, faction, helper, item, state])).
reads(label('Item', [archetype, item_kind])).
reads(rel('PART_OF')).
reads(rel('FACTION_REL')).
reads(rel('STANDING')).
reads(rel('CONTROLS')).
reads(rel('KNOWS_FACT')).
reads(rel('GIVES_QUEST')).
reads(rel('LOCATED_AT')).

% ---------------------------------------------------------------------
% 种类 → 默认特性
% ---------------------------------------------------------------------

kind_traits(family,    [kin, closed, homebound, feud]).
kind_traits(town,      [entry(open, 1), homebound, enduring, broad]).
kind_traits(guild,     [entry(sponsor, 4), trade_guild]).
kind_traits(crew,      [close_knit, entry(sponsor, 3), trade_guild]).
kind_traits(gang,      [close_knit, entry(trial, 4), extorts]).
kind_traits(order,     [entry(trial, 5)]).
kind_traits(authority, [entry(trial, 5), polices]).
kind_traits(other,     [entry(sponsor, 4)]).
kind_traits(band,      [close_knit, entry(sponsor, 4), roving, mood_bound, feud, informal, min_members(2)]).
kind_traits(K, [attacks_on_sight, roving, closed, informal]) :-
    memberchk(K, [monster, monsters, bandit, bandits, beast, beasts, undead, raider, raiders]).

% 特性的机制：trait(特性, 键, 值)。数值键按 mult（连乘）或 sum（相加）使用，见下面的词汇。
%   bond          同属这个群体的人彼此亲近多少（没有就按规模：小 3、中 2、大 1）
%   claim         占地盘的快慢（roving 的 0 = 不占地盘）
%   contest       和对头同处一地时削弱对方的力度
%   escalate      摩擦变成敌意的快慢
%   join          入会门槛的加减
%   recruit       吸收外人的门槛加减
%   loyalty       成员忠诚每轮的涨落
%   pos_ripple / neg_ripple   别人对成员的好意 / 恶意传到声望上的放大
%   attack_penalty            谴责任何动手的人
% 没有键的特性是开关：closed（外人进不来）homebound（忠诚不看地盘）enduring（不解散、不升格）
% broad（不分裂）mood_bound（立场跟着成员好感走）feud（对敌对群体的人见面就打）informal（不进编年史）
% attacks_on_sight（见人就打）extorts（收保护费）polices（管治安）trade_guild（护着成员的生意）
trait(kin, bond, 4).
trait(close_knit, bond, 3).
trait(roving, claim, 0).
trait(expansionist, claim, 1.5).
trait(insular, claim, 0.7).
trait(insular, join, 2.0).
trait(welcoming, join, -2.0).
trait(welcoming, pos_ripple, 1.25).
trait(welcoming, recruit, 2.0).
trait(militant, contest, 1.5).
trait(militant, escalate, 1.25).
trait(pacifist, escalate, 0.5).
trait(pacifist, attack_penalty, 1.0).
trait(vengeful, neg_ripple, 1.5).
trait(vengeful, escalate, 1.5).
trait(mercantile, pos_ripple, 1.5).
trait(mercantile, escalate, 0.75).
trait(zealous, loyalty, 0.2).
trait(zealous, escalate, 1.25).
trait(secretive, join, 1.0).

% 可以由剧情给群体添上的特性（LLM 写新群体时从这里挑）
trait_text(expansionist, "always reaching for new ground").
trait_text(insular, "keeps to its own and its own ground").
trait_text(welcoming, "opens its doors to newcomers").
trait_text(militant, "settles things by force").
trait_text(pacifist, "abhors violence").
trait_text(vengeful, "never forgets a wrong").
trait_text(mercantile, "values trade, gifts and fair dealing").
trait_text(zealous, "burns with conviction").
trait_text(secretive, "guards its business closely").

% ---------------------------------------------------------------------
% 常量
% ---------------------------------------------------------------------

const(inherited_loyalty, 0.5).   % 上级群体的忠诚 = 直接群体的忠诚 × 0.5^层数
const(standing_weight, 0.3).
const(turf_penalty, 1.0).
const(turf_here_bonus, 1.25).    % 在某群体自己的地盘上发生的事，它看得更清楚
const(betrayal, 1.5).            % 自己人对自己人下手
const(sponsor_min, 3.0).         % 担保人对你的倾向至少这么多
const(rival_join_penalty, 2.0).
const(recruit_loyalty, 5.0).
const(promote_standing, 7.0).
const(leave_penalty, -3.0).
const(deed(give), 2.0).
const(deed(attack), -6.0).

stance_score(allied, 3.0).
stance_score(friendly, 1.0).
stance_score(neutral, 0.0).
stance_score(rival, -2.0).
stance_score(hostile, -4.0).

% 对某成员做的事，多少传到其群体的声望上（规模越小，越当成自己的事）
ripple_share(small, 0.5).
ripple_share(medium, 0.3).
ripple_share(large, 0.15).
scale_weight(small, 1.0).
scale_weight(medium, 0.8).
scale_weight(large, 0.6).
stance_ripple(allied, 0.5).
stance_ripple(friendly, 0.25).
stance_ripple(rival, -0.25).
stance_ripple(hostile, -0.5).
join_shift(allied, 0.5).
join_shift(friendly, 0.25).
join_shift(rival, -1.0).
join_shift(hostile, -2.0).

leader_word(W) :- memberchk(W, [boss, leader, head, elder, sergeant, captain, master, chief]).

% ---------------------------------------------------------------------
% 词汇
% ---------------------------------------------------------------------

group(G) :- node('Faction', G), ( prop(G, status, S) -> S == active ; true ).
kind(G, K) :- ( prop(G, kind, K0), K0 \== '' -> K = K0 ; K = other ).
scale(G, S) :- ( prop(G, scale, S0), ripple_share(S0, _) -> S = S0 ; S = medium ).

living(P) :- node('Person', P), \+ prop(P, dead, true), ( prop(P, hp, H) -> H > 0 ; true ).
location(P, L) :- once(rel('LOCATED_AT', P, L)).

kv(Ps, K, D, V) :- ( memberchk(K-V0, Ps), V0 \== '' -> V = V0 ; V = D ).

% 直接成员
direct(P, G, Role, Loyalty) :-
    rel('MEMBER_OF', P, G, Ps), group(G),
    kv(Ps, role, member, Role), kv(Ps, loyalty, 6.0, Loyalty).
direct(P, G) :- direct(P, G, _, _).

above(G, Up, 1) :- rel('PART_OF', G, Up).
above(G, Up, D) :- rel('PART_OF', G, M), above(M, Up, D0), D0 < 3, D is D0 + 1.

% P 属于 G（直接，或经由 Via 继承的上级群体）
member(P, G, Loyalty, '') :- direct(P, G, _, Loyalty).
member(P, Up, Loyalty, Via) :-
    setof(U, F^D^(direct(P, F), above(F, U, D), group(U)), Ups),
    member(Up, Ups), \+ direct(P, Up),
    const(inherited_loyalty, K),
    aggregate_all(max(L1, F1), (direct(P, F1, _, L0), above(F1, Up, D), L1 is L0 * K ** D), max(Loyalty, Via)).
in_group(P, G) :- member(P, G, _, _).

% 排好序的成员身份：直接的在前，忠诚高的在前
memberships(P, Ms) :-
    findall(k(Ind, NL, G)-m(G, L, Via), (member(P, G, L, Via), ( Via == '' -> Ind = 0 ; Ind = 1 ), NL is -L), Ks),
    keysort(Ks, S), pairs_values(S, Ms).

role(P, G, Role) :- ( direct(P, G, Role, _) -> true ; Role = member ).

% 特性 = 种类默认 + 自己写的（JSON：["militant"] 或 [{"id": "militant", "why": "..."}]）
traits(G, Ts) :-
    kind(G, K), ( kind_traits(K, KT) -> true ; KT = [] ),
    findall(T, own_trait(G, T, _), Own), append(KT, Own, Ts).

own_trait(G, T, Why) :-
    prop(G, traits, Raw), trait_list(Raw, L), member(X, L), trait_entry(X, T, Why).

trait_list(Raw, L) :- is_list(Raw), !, L = Raw.
trait_list(Raw, L) :- atom(Raw), catch(atom_json_dict(Raw, L0, []), _, fail), is_list(L0), !, L = L0.
trait_list(_, []).

trait_entry(X, T, "") :- ( atom(X) ; string(X) ), !, atom_string(T, X).
trait_entry(X, T, Why) :- is_dict(X), get_dict(id, X, Id), atom_string(T, Id),
    ( get_dict(why, X, W) -> Why = W ; Why = "" ).

has(G, T) :- traits(G, Ts), memberchk(T, Ts).

mult(G, K, M) :- traits(G, Ts), foldl(mult_step(K), Ts, 1.0, M).
mult_step(K, T, A, B) :- ( trait(T, K, V), number(V) -> B is A * V ; B = A ).
sum(G, K, S) :- traits(G, Ts), foldl(sum_step(K), Ts, 0.0, S).
sum_step(K, T, A, B) :- ( trait(T, K, V), number(V) -> B is A + V ; B = A ).

bond(G, B) :- traits(G, Ts), aggregate_all(max(V), (member(T, Ts), trait(T, bond, V)), B), !.
bond(G, B) :- scale(G, S), nth1(I, [small, medium, large], S), nth1(I, [3.0, 2.0, 1.0], B).

% 入会规矩：群体自己写的 join_policy / join_standing 优先，其次特性 entry(规矩, 门槛)，closed 就进不来
entry(G, closed, 0.0) :- has(G, closed), \+ prop(G, join_policy, _), !.
entry(G, Policy, Threshold) :-
    traits(G, Ts), ( member(entry(P0, T0), Ts) -> true ; P0 = sponsor, T0 = 4 ),
    ( prop(G, join_policy, P1), memberchk(P1, [open, sponsor, trial, closed]) -> P2 = P1 ; P2 = P0 ),
    ( prop(G, join_standing, T1), number(T1) -> T2 = T1 ; T2 = T0 ),
    ( P2 == trial, \+ trial_of(G, _, _) -> Policy = sponsor ; Policy = P2 ),
    Threshold is float(T2).

trial_of(G, Item, Text) :-
    prop(G, trial, Raw), atom(Raw), catch(atom_json_dict(Raw, D, []), _, fail),
    get_dict(item, D, I), I \== "", atom_string(Item, I),
    ( get_dict(text, D, Text) -> true ; Text = "" ).

% 首领：成员关系上写了 leader: true 的人；否则角色像首领的、忠诚最高的（新人不算）
leader_of(G, P) :- rel('MEMBER_OF', P, G, Ps), memberchk(leader-true, Ps), !.
leader_of(G, P) :-
    findall(R-L-Q, (direct(Q, G, Role, L), Role \== recruit, leader_rank(Role, R)), Ks), Ks \== [],
    max_member(_-_-P, Ks).
leader_rank(Role, 1) :- atom(Role), downcase_atom(Role, R), leader_word(W), sub_atom(R, _, _, _, W), !.
leader_rank(_, 0).

group_rel(A, B, Ps) :- A \== B, once(( rel('FACTION_REL', A, B, Ps) ; rel('FACTION_REL', B, A, Ps) )).
stance(A, B, S) :- group_rel(A, B, Ps), kv(Ps, stance, neutral, S).
stances_of(G, O, S) :-
    group(G), setof(X, P^( rel('FACTION_REL', G, X, P) ; rel('FACTION_REL', X, G, P) ), Os),
    member(O, Os), O \== G, group(O), stance(G, O, S).
foe(S) :- memberchk(S, [rival, hostile]).

standing(P, G, V) :- ( rel_prop('STANDING', P, G, value, V0) -> V = V0 ; V = 0.0 ).

standing_label(V, trusted) :- V >= 6, !.
standing_label(V, welcome) :- V >= 3, !.
standing_label(V, unknown) :- V > -1, !.
standing_label(V, distrusted) :- V > -4, !.
standing_label(_, hated).

attitude_label(S, 'loyal / protective') :- S >= 4, !.
attitude_label(S, friendly) :- S >= 1.5, !.
attitude_label(S, neutral) :- S > -1.5, !.
attitude_label(S, wary) :- S > -4, !.
attitude_label(_, hostile).

controller(L, G) :- prop(L, controlled_by, G), G \== '', G \== null.

strength(G, L, S) :- rel_prop('CONTROLS', G, L, strength, S).
territory(G, Pairs) :-
    findall(NS-L, (strength(G, L, S), NS is -S), Ks), keysort(Ks, Sorted),
    findall(L-S, (member(NS-L, Sorted), S is -NS), Pairs).

signed(V, A) :- ( V >= 0 -> format(atom(A), "+~1f", [V]) ; format(atom(A), "~1f", [V]) ).
round1(X, Y) :- Y is round(X * 10) / 10.

% ---------------------------------------------------------------------
% 亲疏：Me 因为群体关系对 Other 的倾向（-10..10）和理由
% ---------------------------------------------------------------------

attitude(Me, Other, Score, Reasons) :-
    findall(G-L, member(Me, G, L, _), Mine),
    findall(G, member(Other, G, _, _), Theirs),
    findall(P-R, attitude_part(Me, Other, Mine, Theirs, P, R), Parts),
    pairs_keys_values(Parts, Ps, Reasons),
    sum_list(Ps, S0), S1 is max(-10.0, min(10.0, S0)), round1(S1, Score).

attitude_part(_, _, Mine, Theirs, P, R) :-          % 同属一个群体
    member(G-L, Mine), memberchk(G, Theirs),
    bond(G, B), P is B * L / 10, kind(G, K), format(atom(R), "both of ~w (~w)", [G, K]).
attitude_part(_, _, Mine, Theirs, P, R) :-          % 我方和对方的群体之间的立场
    member(A-L, Mine), \+ memberchk(A, Theirs),
    member(B, Theirs), \+ memberchk(B-_, Mine),
    stance(A, B, S), stance_score(S, K), K =\= 0,
    scale(A, Sc), scale_weight(Sc, W), P is K * L / 10 * W,
    format(atom(R), "your ~w is ~w with their ~w", [A, S, B]).
attitude_part(_, Other, Mine, Theirs, P, R) :-      % 对方在我方群体里的名声
    member(G-L, Mine), \+ memberchk(G, Theirs),
    standing(Other, G, V), const(standing_weight, W), P is V * W * L / 10, abs(P) >= 0.1,
    standing_label(V, Lab), format(atom(R), "~w thinks of them as ~w", [G, Lab]).
attitude_part(Me, _, Mine, Theirs, P, R) :-         % 在谁的地盘上碰见
    location(Me, Here), controller(Here, C),
    (   memberchk(C-_, Mine), member(T, Theirs), stance(C, T, S), foe(S)
    ->  const(turf_penalty, K), P is -K, format(atom(R), "they are on your ~w's ground", [C])
    ;   memberchk(C, Theirs), member(M-_, Mine), stance(C, M, S), foe(S)
    ->  const(turf_penalty, K), P is -K / 2, format(atom(R), "you are on their ~w's ground", [C])
    ).

% ---------------------------------------------------------------------
% 提示词
% ---------------------------------------------------------------------

membership_line(P, m(G, L, Via), Line) :-
    kind(G, K), scale(G, S), role(P, G, Role),
    ( Via == '' -> ViaT = '' ; format(atom(ViaT), ", through ~w", [Via]) ),
    ( prop(G, goal, Goal), Goal \== '' -> format(atom(GoalT), " — goal: ~w", [Goal]) ; GoalT = '' ),
    format(atom(Line), "~w (~w, ~w; ~w, loyalty ~0f/10~w)~w", [G, K, S, Role, L, ViaT, GoalT]).

detail_line(G, T) :-
    findall(Txt, ( own_trait(G, Tr, Why), trait_text(Tr, _),
                   ( Why == "" -> format(atom(Txt), "~w", [Tr]) ; format(atom(Txt), "~w (~w)", [Tr, Why]) ) ), Bits),
    Bits \== [], atomic_list_concat(Bits, '; ', B), format(atom(T), "  ~w's character: ~w", [G, B]).
detail_line(G, T) :-
    territory(G, Pairs), Pairs \== [], length(Pairs, N), Top is min(4, N), length(Head, Top), append(Head, _, Pairs),
    findall(X, (member(L-S, Head), format(atom(X), "~w (~0f/10)", [L, S])), Xs),
    atomic_list_concat(Xs, ', ', J), format(atom(T), "  ~w holds: ~w", [G, J]).
detail_line(G, T) :-
    prop(G, last_news, News), News \== '', ( prop(G, last_news_day, D) -> true ; D = '?' ),
    format(atom(T), "  Lately (day ~w): ~w", [D, News]).

top(N, L, H) :- length(L, Len), K is min(N, Len), length(H, K), append(H, _, L).

% 决策时的处境
context(Me, T) :- situation(Me, T).

situation(Me, "Your factions (their goals are yours too, as far as your loyalty goes):") :-
    once(member(Me, _, _, _)).
situation(Me, T) :-
    memberships(Me, Ms), top(4, Ms, Top), member(M, Top), M = m(G, _, Via),
    (   membership_line(Me, M, L), format(atom(T), "- ~w", [L])
    ;   Via == '', detail_line(G, T)
    ;   Via == '', findall(X, (stances_of(G, O, S), foe(S), format(atom(X), "~w (~w)", [O, S])), Xs), Xs \== [],
        atomic_list_concat(Xs, ', ', J), format(atom(T), "  ~w opposes: ~w", [G, J])
    ).
situation(Me, "Territory: members standing in a place strengthen their faction's hold on it; unwatched ground slips away, and rivals in the same place wear each other down.") :-
    once(member(Me, _, _, _)).
situation(Me, T) :-
    location(Me, Here), controller(Here, C), format(atom(T), "You are standing on ~w's ground (~w).", [C, Here]).
situation(Me, T) :-
    location(Me, Here), setof(O, (rel('LOCATED_AT', O, Here), O \== Me, node('Person', O)), Os), member(O, Os),
    attitude(Me, O, S, Rs), Rs \== [], attitude_label(S, Lab), top(3, Rs, R3),
    atomic_list_concat(R3, '; ', J), format(atom(T), "~w: you lean ~w — ~w", [O, Lab, J]).

% 面对某人时的关系
context(Me, Other, T) :-
    once(( member(Me, _, _, _) ; member(Other, _, _, _) )),
    findall(X, facing(Me, Other, X), Xs), member(T, Xs).

facing(Me, _, T) :-
    memberships(Me, Ms),
    (   Ms == [] -> T = "Your factions: none — you stand alone."
    ;   (   T = "Your factions:"
        ;   top(4, Ms, Top), member(M, Top), M = m(G, _, Via),
            ( membership_line(Me, M, L), format(atom(T), "- ~w", [L]) ; Via == '', detail_line(G, T) )
        )
    ).
facing(_, Other, T) :-
    memberships(Other, Ms),
    (   Ms == [] -> format(atom(T), "~w belongs to no faction you know of.", [Other])
    ;   top(4, Ms, Top), findall(X, (member(m(G, _, _), Top), kind(G, K), role(Other, G, R),
                                     format(atom(X), "~w (~w, ~w)", [G, K, R])), Xs),
        atomic_list_concat(Xs, '; ', J), format(atom(T), "~w's factions: ~w", [Other, J])
    ).
facing(Me, _, T) :- location(Me, Here), controller(Here, C), format(atom(T), "This place (~w) is held by ~w.", [Here, C]).
facing(Me, Other, T) :- direct_or_up(Me, G), in_group(Other, G), format(atom(T), "You and ~w are both of ~w.", [Other, G]).
facing(Me, Other, T) :-
    findall(G, member(Me, G, _, _), Mine), findall(G, member(Other, G, _, _), Theirs),
    member(A, Mine), \+ memberchk(A, Theirs), member(B, Theirs), \+ memberchk(B, Mine),
    group_rel(A, B, Ps), kv(Ps, stance, neutral, S),
    ( kv(Ps, reason, '', Why), Why \== '' -> format(atom(W), " — ~w", [Why]) ; W = '' ),
    format(atom(T), "Your ~w is ~w with their ~w~w.", [A, S, B, W]).
facing(Me, Other, T) :-
    member(Me, G, _, _), \+ in_group(Other, G), standing(Other, G, V), abs(V) >= 0.5,
    standing_label(V, L), signed(V, SV), format(atom(T), "~w regards ~w as ~w (~w).", [G, Other, L, SV]).
facing(Me, Other, T) :-
    attitude(Me, Other, S, _), attitude_label(S, L), signed(S, SS),
    format(atom(T), "Because of all this you lean ~w toward ~w (faction score ~w).", [L, Other, SS]).
facing(Me, Other, T) :- player(Other), findall(H, join_hint(Me, Other, H), Hs), top(2, Hs, H2), member(T, H2).
facing(Me, Other, T) :-
    findall(L, secret_line(Me, Other, L), Ls), Ls \== [],
    (   T = "Faction matters you know:"
    ;   top(4, Ls, L4), member(X, L4), format(atom(T), "- ~w", [X])
    ).
facing(_, _, "Faction loyalty shapes this talk: protect your own people, never hand rivals your side's secrets, and remember that what is done to one member, their whole faction hears about. Family and gang ties are personal; town-wide ties are looser.").

direct_or_up(P, G) :- member(P, G, _, _).

join_hint(Me, Other, T) :-
    direct(Me, G), \+ in_group(Other, G),
    join_check(Other, G, Me, Policy, _, _, Blockers, _, Item, _), Policy \== closed,
    (   Blockers == []
    ->  (   Policy == open -> How = "you can simply welcome them"
        ;   Policy == sponsor -> How = "you are willing to vouch for them"
        ;   format(atom(How), "they would first have to bring ~w to your leader", [Item])
        ),
        format(atom(T), "If ~w asks to join ~w: ~w.", [Other, G, How])
    ;   Blockers = [B|_], format(atom(T), "If ~w asks to join ~w: not yet — ~w.", [Other, G, B])
    ).

secret_line(Me, Other, T) :-
    rel('KNOWS_FACT', Me, F), prop(F, faction, Fac), Fac \== '',
    (   in_group(Other, Fac) -> How = "they are one of yours; you may share it"
    ;   once(( member(Other, G, _, _), stance(Fac, G, S), stance_score(S, K), K =\= 0 )) ->
        (   foe(S) -> format(atom(How), "they side with ~w's enemies; never tell them", [Fac])
        ;   How = "an ally; share if it helps both sides"
        )
    ;   How = "an outsider; only if they have earned real trust"
    ),
    format(atom(T), "[~w] belongs to ~w: ~w", [F, Fac, How]).

membership_summary(P, Text) :-
    memberships(P, Ms),
    findall(X, (member(m(G, _, Via), Ms), kind(G, K), role(P, G, R),
                ( Via == '' -> format(atom(X), "~w (~w, ~w)", [G, K, R]) ; format(atom(X), "~w (~w, ~w, via ~w)", [G, K, R, Via]) )), Xs),
    atomic_list_concat(Xs, '; ', Text).

% ---------------------------------------------------------------------
% 对人做的事传到他的群体：送东西（+2）、打人（-6）、别的好意恶意（deed：谈话、兑现人情……）
% ---------------------------------------------------------------------

after(give(A, I, B, L), E) :-
    const(deed(give), W), format(atom(Why), "gave ~w to ~w", [I, B]), deed_effects(A, B, W, Why, L, Es), member(E, Es).
after(attack(A, B, L), E) :-
    const(deed(attack), W), format(atom(Why), "attacked ~w", [B]), condemned(A, B, Cs),
    deed_effects(A, B, W, Why, L, Cs, Es), member(E, Es).
after(attack(A, B, _), E) :- condemnations(A, B, Es), member(E, Es).
after(deed(A, B, W, Why), E) :- deed_effects(A, B, W, Why, '', Es), member(E, Es).

% 和平主义的群体谴责任何动手的人，不管打的是谁
condemned(A, B, Cs) :-
    findall(G-D, (group(G), sum(G, attack_penalty, Pen), Pen > 0, D is -Pen, G \== B, A \== B), Cs).
condemnations(A, B, Es) :-
    condemned(A, B, Cs),
    findall(E, ( member(G-D, Cs), standing(A, G, V0), V1 is max(-10.0, min(10.0, V0 + D)),
                 (   E = _{op: add_rel_prop, a: A, a_label: 'Person', b: G, b_label: 'Faction', rel: 'STANDING',
                           prop: value, delta: D, min: -10.0, max: 10.0}
                 ;   signed(V0, S0), signed(V1, S1),
                     format(atom(T), "~w condemns the violence against ~w: ~w ~w→~w", [G, B, A, S0, S1]),
                     E = _{op: note, text: T}
                 ) ), Es).

deed_effects(A, B, W, Why, Where, Es) :- deed_effects(A, B, W, Why, Where, [], Es).

% 先已有的变动（Pre：同一件事里别的规则已经改了的声望），再算这次的
deed_effects(A, B, W, Why, Where, Pre, Es) :-
    A \== B, W =\= 0, findall(G, member(B, G, _, _), Targets), Targets \== [], !,
    findall(F-D, ripple(A, B, W, Targets, F, D), Raw),
    sum_by_key(Raw, Changes0),
    findall(F-D, ( member(F-D0, Changes0),
                   ( D0 > 0 -> mult(F, pos_ripple, M) ; mult(F, neg_ripple, M) ),
                   ( Where \== '', controller(Where, F) -> const(turf_here_bonus, H) ; H = 1.0 ),
                   D is D0 * M * H, abs(D) >= 0.05 ), Changes),
    findall(E, deed_effect(A, Why, Pre, Changes, E), Es).
deed_effects(_, _, _, _, _, _, []).

ripple(A, B, W, _, F, D) :-                       % 对方自己的群体
    member(B, F, _, Via), scale(F, S), ripple_share(S, Sh0),
    ( Via == '' -> Sh1 = Sh0 ; const(inherited_loyalty, K), Sh1 is Sh0 * K ),
    ( W < 0, in_group(A, F) -> const(betrayal, Bt), Sh is Sh1 * Bt ; Sh = Sh1 ),
    D is W * Sh.
ripple(_, B, W, Targets, O, D) :-                 % 和对方群体结盟 / 交恶的群体
    direct(B, F), scale(F, S), ripple_share(S, Sh),
    stances_of(F, O, St), stance_ripple(St, K), \+ memberchk(O, Targets),
    D is W * Sh * K.

sum_by_key(Pairs, Sums) :-
    findall(K, member(K-_, Pairs), Ks0), sort(Ks0, Ks),
    findall(K-S, (member(K, Ks), aggregate_all(sum(V), member(K-V, Pairs), S)), Sums).

deed_effect(A, _, _, Changes, _{op: add_rel_prop, a: A, a_label: 'Person', b: F, b_label: 'Faction', rel: 'STANDING',
                                prop: value, delta: D, min: -10.0, max: 10.0}) :-
    member(F-D, Changes).
deed_effect(A, Why, Pre, Changes, _{op: note, text: T}) :-
    member(F-D, Changes),
    standing(A, F, V00), ( memberchk(F-P, Pre) -> V0 is max(-10.0, min(10.0, V00 + P)) ; V0 = V00 ),
    V1 is max(-10.0, min(10.0, V0 + D)), abs(V1 - V0) >= 0.5,
    signed(V0, S0), signed(V1, S1), format(atom(T), "~w: ~w ~w→~w (~w)", [F, A, S0, S1, Why]).
deed_effect(A, _, Pre, Changes, E) :-                   % 新人声望够了：升为正式成员
    Changes \== [], direct(A, F, recruit, _),
    standing(A, F, V0),
    ( memberchk(F-D1, Changes) -> true ; D1 = 0 ), ( memberchk(F-D2, Pre) -> true ; D2 = 0 ),
    V is min(10.0, V0 + D1 + D2), const(promote_standing, Min), V >= Min,
    (   E = _{op: set_rel_prop, a: A, a_label: 'Person', b: F, b_label: 'Faction', rel: 'MEMBER_OF', prop: role, value: member}
    ;   E = _{op: set_rel_prop, a: A, a_label: 'Person', b: F, b_label: 'Faction', rel: 'MEMBER_OF', prop: loyalty, value: 7.0}
    ;   format(atom(T), "~w is now a full member of ~w", [A, F]), E = _{op: note, text: T}
    ).

% ---------------------------------------------------------------------
% 入会、退出
%   open     声望够了，找任意一个在场成员开口即可
%   sponsor  声望够了，还要有在场成员愿意替你担保
%   trial    声望够了，首领出一道考验（把某样东西亲手交给他）；交到才算入会
%   closed   外人进不来
% 与该群体敌对的群体成员一律不收；身在其对头里，门槛 +2。
% ---------------------------------------------------------------------

join_check(P, G, Via, Policy, Threshold, Standing, Blockers, Sponsors, Item, Text) :-
    group(G), entry(G, Policy, T0), sum(G, join, Tj),
    findall(Pen, (stances_of(G, O, rival), direct(P, O), const(rival_join_penalty, Pen)), Pens), sum_list(Pens, Tr),
    Threshold is T0 + Tj + Tr,
    standing(P, G, Standing),
    ( trial_of(G, Item, Text) -> true ; Item = '', Text = "" ),
    ( location(P, Here) -> true ; Here = '' ),
    findall(M, ( direct(M, G, Role, _), M \== P, Role \== recruit, Here \== '', location(M, Here),
                 ( Via == '' ; M == Via ) ), Present0), sort(Present0, Present),
    findall(M, ( member(M, Present), willing(M, P, Policy) ), Sponsors),
    findall(B, blocker(P, G, Via, Policy, Threshold, Standing, Present, Sponsors, B), Blockers).

willing(M, P, sponsor) :- !, attitude(M, P, S, _), personal_trust(M, P, T), S + T >= 3.0.
willing(_, _, _).

personal_trust(M, P, T) :- player(P), \+ player(M), !, ( prop(M, trust_toward_player, T) -> true ; T = 0 ).
personal_trust(_, _, 0).

blocker(P, G, _, _, _, _, _, _, "already a member") :- direct(P, G).
blocker(_, G, _, closed, _, _, _, _, B) :- kind(G, K), format(atom(B), "~w is a ~w; outsiders cannot simply join", [G, K]).
blocker(P, G, _, _, _, _, _, _, B) :-
    stances_of(G, O, hostile), direct(P, O), format(atom(B), "you belong to ~w, which ~w is hostile to", [O, G]).
blocker(_, _, _, _, T, S, _, _, B) :- S < T, signed(S, SS), signed(T, ST),
    format(atom(B), "standing ~w is below the ~w they require", [SS, ST]).
blocker(_, G, Via, _, _, _, [], _, B) :-
    (   Via == '' -> format(atom(B), "no member of ~w is here to ask", [G])
    ;   format(atom(B), "~w is not a member of ~w here", [Via, G])
    ).
blocker(_, _, _, _, _, _, [_|_], [], "nobody here is willing to vouch for you").
blocker(P, G, _, trial, _, _, _, _, "your trial is already under way") :- open_trial(P, G, _, _).

open_trial(P, G, Q, Giver) :-
    node('Quest', Q), prop(Q, kind, trial), prop(Q, faction, G), prop(Q, helper, P),
    \+ ( prop(Q, state, St), St \== active ), rel('GIVES_QUEST', Giver, Q).

check_dict(P, G, Via, _{policy: Po, threshold: Th, standing: St1, can_join: Ok, blockers: Bs,
                        sponsors_here: Sp, trial_item: I, trial_text: Tx}) :-
    join_check(P, G, Via, Po, Th, St, Bs, Sp, I, Tx), round1(St, St1),
    ( Bs == [] -> Ok = true ; Ok = false ).

after(ask_to_join(P, G, Via), E) :-
    (   \+ group(G)
    ->  format(atom(T), "Unknown faction ~w", [G]),
        E = _{op: report, ok: false, outcome: refused, faction: G, notes: [T]}
    ;   check_dict(P, G, Via, C), join_effects(P, G, C, Es), member(E, Es)
    ).

join_effects(_, G, C, [_{op: report, ok: false, outcome: refused, faction: G, notes: C.blockers, check: C}]) :-
    C.can_join == false, !.
join_effects(P, G, C, Es) :-
    C.policy == trial, !,
    C.sponsors_here = [Sponsor|_], ( leader_of(G, Giver) -> true ; Giver = Sponsor ),
    atom_concat('Trial', G, Q),
    format(atom(Obj), "Hand ~w to ~w yourself to be accepted into ~w", [C.trial_item, Giver, G]),
    ( C.trial_text \== "" -> Desc = C.trial_text ; format(atom(Desc), "~w wants proof before they take you in.", [G]) ),
    format(atom(Mem), "~w said ~w might take you in — but first: ~w.", [Sponsor, G, Obj]),
    Es = [ _{op: merge_node, label: 'Quest', name: Q, props: _{kind: trial}},
           _{op: set_prop, entity: Q, label: 'Quest', prop: faction, value: G},
           _{op: set_prop, entity: Q, label: 'Quest', prop: helper, value: P},
           _{op: set_prop, entity: Q, label: 'Quest', prop: item, value: C.trial_item},
           _{op: set_prop, entity: Q, label: 'Quest', prop: state, value: active},
           _{op: set_prop, entity: Q, label: 'Quest', prop: stage, value: asked},
           _{op: set_prop, entity: Q, label: 'Quest', prop: objective, value: Obj},
           _{op: set_prop, entity: Q, label: 'Quest', prop: description, value: Desc},
           _{op: link, a: Giver, a_label: 'Person', b: Q, b_label: 'Quest', rel: 'GIVES_QUEST'},
           _{op: link, a: P, a_label: 'Person', b: Q, b_label: 'Quest', rel: 'ACCEPTED_QUEST'},
           _{op: remember, who: P, text: Mem, type: faction, faction: G},
           _{op: report, ok: true, outcome: trial_started, faction: G, quest: Q, giver: Giver, notes: [Obj], check: C} ].
join_effects(P, G, C, Es) :-
    C.sponsors_here = [Sponsor|_],
    ( C.policy == sponsor -> Via = Sponsor ; Via = '' ),
    admit(P, G, Via, Admit),
    append(Admit, [_{op: report, ok: true, outcome: joined, faction: G, check: C}], Es).

% 收为新人：声望 +1，盟友更喜欢你、对头更讨厌你，知道该群体的内情
admit(P, G, Via, Es) :-
    const(recruit_loyalty, L),
    ( Via == '' -> format(atom(N0), "~w joined ~w as a recruit", [P, G])
    ; format(atom(N0), "~w joined ~w as a recruit (sponsored by ~w)", [P, G, Via]) ),
    findall(F-Txt, (rel('KNOWS_FACT', _, F), prop(F, faction, G), \+ rel('KNOWS_FACT', P, F),
                    ( prop(F, text, Txt) -> true ; Txt = F )), Fs0), sort(Fs0, Fs),
    findall(E, admit_effect(P, G, Via, L, N0, Fs, E), Es).

admit_effect(P, G, Via, L, _, _, _{op: link, a: P, a_label: 'Person', b: G, b_label: 'Faction', rel: 'MEMBER_OF',
                                   props: _{role: recruit, loyalty: L, sponsor: Via}}).
admit_effect(_, _, _, _, N0, _, _{op: note, text: N0}).
admit_effect(P, G, _, _, _, _, _{op: add_rel_prop, a: P, a_label: 'Person', b: G, b_label: 'Faction', rel: 'STANDING',
                                 prop: value, delta: 1.0, min: -10.0, max: 10.0}).
admit_effect(P, G, _, _, _, _, E) :-
    stances_of(G, O, S), join_shift(S, D), standing(P, O, V0), V1 is max(-10.0, min(10.0, V0 + D)),
    (   E = _{op: add_rel_prop, a: P, a_label: 'Person', b: O, b_label: 'Faction', rel: 'STANDING',
              prop: value, delta: D, min: -10.0, max: 10.0}
    ;   signed(V0, S0), signed(V1, S1), format(atom(T), "~w (~w of ~w): ~w→~w", [O, S, G, S0, S1]),
        E = _{op: note, text: T}
    ).
admit_effect(P, _, _, _, _, Fs, _{op: link, a: P, a_label: 'Person', b: F, b_label: 'Fact', rel: 'KNOWS_FACT'}) :-
    member(F-_, Fs).
admit_effect(_, G, _, _, _, Fs, _{op: note, text: T}) :-
    Fs \== [], findall(F, member(F-_, Fs), Ns), atomic_list_concat(Ns, ', ', J),
    format(atom(T), "learned ~w's business: ~w", [G, J]).
admit_effect(P, G, Via, _, _, Fs, _{op: remember, who: P, text: T, type: faction, faction: G}) :-
    ( Via == '' -> format(atom(T0), "You joined ~w.", [G]) ; format(atom(T0), "You joined ~w; ~w vouched for you.", [G, Via]) ),
    findall(X, (member(_-Txt, Fs), format(atom(X), " They told you: ~w", [Txt])), Xs),
    atomic_list_concat([T0|Xs], T).
admit_effect(P, G, Via, _, _, _, _{op: remember, who: Via, text: T, type: faction, faction: G}) :-
    Via \== '', format(atom(T), "~w joined ~w, and you vouched for them.", [P, G]).

% 亲手把考验要的东西交给出题人 → 入会
after(give(A, I, B, _), E) :-
    open_trial(A, G, Q, B), prop(Q, item, Want), item_matches(I, Want),
    format(atom(N), "~w passed", [Q]),
    (   E = _{op: set_prop, entity: Q, label: 'Quest', prop: state, value: completed}
    ;   E = _{op: set_prop, entity: Q, label: 'Quest', prop: stage, value: done}
    ;   E = _{op: note, text: N}
    ;   admit(A, G, B, Es), member(E, Es)
    ).

item_matches(I, I) :- !.
item_matches(I, Want) :-
    downcase_atom(Want, W),
    ( prop(I, archetype, A) ; prop(I, item_kind, A) ), atom(A), downcase_atom(A, W), !.

after(leave_group(P, G), E) :-
    (   direct(P, G)
    ->  const(leave_penalty, D), standing(P, G, V0), V1 is max(-10.0, min(10.0, V0 + D)),
        signed(V0, S0), signed(V1, S1),
        format(atom(N1), "~w left ~w", [P, G]), format(atom(N2), "~w: ~w→~w", [G, S0, S1]),
        format(atom(M), "You walked away from ~w. They will not forget it.", [G]),
        member(E, [ _{op: unlink, a: P, a_label: 'Person', b: G, b_label: 'Faction', rel: 'MEMBER_OF'},
                    _{op: add_rel_prop, a: P, a_label: 'Person', b: G, b_label: 'Faction', rel: 'STANDING',
                      prop: value, delta: D, min: -10.0, max: 10.0},
                    _{op: remember, who: P, text: M, type: faction, faction: G},
                    _{op: report, ok: true, faction: G, notes: [N1, N2]} ])
    ;   format(atom(N), "~w is not a member of ~w", [P, G]),
        E = _{op: report, ok: false, faction: G, notes: [N]}
    ).

% ---------------------------------------------------------------------
% 界面：一张群体卡片（viewer 在其中的身份、声望、能不能加入；成员、地盘、立场、特性）
% ---------------------------------------------------------------------

group_card(Viewer, G, Card) :-
    group(G), kind(G, K), scale(G, S),
    ( prop(G, goal, Goal) -> true ; Goal = '' ),
    ( prop(G, description, Desc) -> true ; Desc = '' ),
    ( prop(G, founded_day, FD) -> true ; FD = null ),
    ( rel('PART_OF', G, Par) -> true ; Par = '' ),
    ( Viewer \== '', member(Viewer, G, _, Via) -> Mem = true, role(Viewer, G, Role), ( Via == '' -> Dir = true ; Dir = false )
    ; Mem = false, Role = '', Dir = false ),
    ( Viewer \== '' -> standing(Viewer, G, V0) ; V0 = 0.0 ), round1(V0, V),
    ( Mem == true -> Lab = member ; standing_label(V, Lab) ),
    ( Mem == false, Viewer \== '', check_dict(Viewer, G, '', C) -> Join = C ; Join = null ),
    findall(N-_{name: N, role: R, loyalty: L, location: Loc},
            (direct(N, G, R, L), ( location(N, Loc) -> true ; Loc = null )), Ms0),
    findall(NL-M, (member(_-M, Ms0), NL is -M.loyalty), Ms1), keysort(Ms1, Ms2), pairs_values(Ms2, Members),
    territory(G, TPairs), dict_pairs(Terr, _, TPairs),
    findall(L, controller(L, G), Ctl0), sort(Ctl0, Controls),
    findall(_{with: O, stance: St, reason: Rs}, (stances_of(G, O, St), group_rel(G, O, Ps), kv(Ps, reason, '', Rs)), Rels),
    findall(_{id: T, why: W}, (own_trait(G, T, W), trait_text(T, _)), Traits),
    traits(G, All),
    Card = _{name: G, kind: K, scale: S, goal: Goal, description: Desc, founded_day: FD, parent: Par,
             member: Mem, role: Role, direct: Dir, standing: V, label: Lab, join: Join,
             members: Members, territory: Terr, controls: Controls, relations: Rels, traits: Traits, has: All}.
