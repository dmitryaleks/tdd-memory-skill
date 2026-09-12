package com.acme;

public final class Tax {

    private Tax() {
    }

    public static int withVat(int amount) {
        return amount + (amount / 5);
    }
}
