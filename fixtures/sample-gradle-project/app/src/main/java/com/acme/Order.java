package com.acme;

import java.util.ArrayList;
import java.util.List;

/** The refactoring target used by the fixture tests. */
public class Order {

    private final List<Integer> lines = new ArrayList<>();

    public void addLine(int amount) {
        lines.add(amount);
    }

    public int total() {
        int sum = 0;
        for (int line : lines) {
            sum += line;
        }
        return sum;
    }

    /** Deliberately inlined pricing rule - this is what gets extracted. */
    public int payable() {
        int total = total();
        if (total >= 100) {
            return total - (total / 10);
        }
        return total;
    }
}
